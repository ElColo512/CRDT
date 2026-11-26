"""CRDT de secuencia basado en posiciones inmutables.

Cada bloque insertado recibe una posicion estable entre sus vecinos visibles. 
Si dos nodos insertan en el mismo lugar durante una particion, ambos calculan la misma posicion 
y el ID del bloque desempata de forma determinista. Las letras de un mismo bloque conservan
su orden y no se intercalan caracter por caracter.

La sincronizacion sigue siendo state-based: cada nodo envia todos sus atomos y
``merge`` conserva la union de atomos y propaga los tombstones.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

AtomId = str
Position = List[int]

# Limite usado por el asignador de posiciones. Al insertar entre dos posiciones
# consecutivas se agrega otro nivel, por lo que siempre existe espacio logico.
_POSITION_MAX = 10**100
_SEED_STEP = 1_000_000


@dataclass
class Atom:
    """Caracter del documento con identidad y posicion inmutables.

    position:
        Ruta numerica que situa el bloque entre sus vecinos.
    block_id:
        ID unico de la edicion que creo el bloque. Sirve como desempate cuando
        dos nodos insertan concurrentemente en la misma posicion.
    offset:
        Posicion del caracter dentro del bloque. Evita que dos frases
        concurrentes se mezclen letra por letra.
    deleted:
        Tombstone. El atomo se conserva para que el borrado pueda propagarse.
    """

    id: AtomId
    position: Position
    block_id: str
    offset: int
    value: str
    deleted: bool = False


class SequenceCRDT:
    """CRDT de texto state-based con posiciones fraccionarias deterministas."""

    def __init__(self, node_id: str, seed: str = "hola mundo!"):
        self.node_id = node_id
        self.counter = 0
        self.atoms: Dict[AtomId, Atom] = {}

        # Todos los nodos crean exactamente los mismos atomos iniciales.
        for index, char in enumerate(seed, 1):
            atom_id = f"seed:{index:06d}"
            self.atoms[atom_id] = Atom(
                id=atom_id,
                position=[index * _SEED_STEP],
                block_id=atom_id,
                offset=0,
                value=char,
                deleted=False,
            )

    def _next_block_id(self) -> str:
        """Genera un ID globalmente unico para una edicion local."""
        self.counter += 1
        return f"{self.node_id}:{self.counter:09d}"

    @staticmethod
    def _position_between(
        left: Optional[Sequence[int]], right: Optional[Sequence[int]]
    ) -> Position:
        """Crea una posicion lexicograficamente situada entre left y right.

        Las posiciones son listas de enteros. Si no queda hueco en un nivel,
        copiamos el limite izquierdo y descendemos un nivel. Esto evita cambiar
        posiciones existentes, algo esencial para que el merge sea seguro.
        """
        left = list(left or [])
        right = list(right or [])
        result: Position = []
        depth = 0

        while True:
            left_digit = left[depth] if depth < len(left) else 0
            right_digit = right[depth] if depth < len(right) else _POSITION_MAX

            if right_digit - left_digit > 1:
                result.append(left_digit + (right_digit - left_digit) // 2)
                return result

            result.append(left_digit)
            depth += 1

    @staticmethod
    def _sort_key(atom: Atom) -> Tuple[Tuple[int, ...], str, int, str]:
        """Orden total identico en todos los nodos."""
        return (tuple(atom.position), atom.block_id, atom.offset, atom.id)

    def ordered_atoms(self) -> List[Atom]:
        """Devuelve todos los atomos, incluidos tombstones, en orden estable."""
        return sorted(self.atoms.values(), key=self._sort_key)

    def visible_atoms(self) -> List[Atom]:
        return [atom for atom in self.ordered_atoms() if not atom.deleted]

    def text(self) -> str:
        return "".join(atom.value for atom in self.visible_atoms())

    def delete(self, atom_id: AtomId) -> None:
        """Marca un caracter como borrado sin eliminar su identidad."""
        if atom_id in self.atoms:
            self.atoms[atom_id].deleted = True

    def _insert_block(
        self,
        left_position: Optional[Sequence[int]],
        right_position: Optional[Sequence[int]],
        value: str,
    ) -> None:
        """Inserta una cadena completa como un bloque indivisible para ordenar."""
        if not value:
            return

        block_id = self._next_block_id()
        base_position = self._position_between(left_position, right_position)

        # El ID completo del bloque se convierte en un entero unico y estable.
        # Al colocarlo antes del offset, dos bloques concurrentes quedan enteros:
        # todas las letras de A se ordenan antes o despues de todas las de B.
        block_rank = int.from_bytes(block_id.encode("utf-8"), "big")

        for offset, char in enumerate(value):
            atom_id = f"{block_id}:{offset:06d}"
            self.atoms[atom_id] = Atom(
                id=atom_id,
                position=list(base_position) + [block_rank, offset + 1],
                block_id=block_id,
                offset=offset,
                value=char,
                deleted=False,
            )

    def merge(self, incoming: Iterable[dict]) -> bool:
        """Une otro estado de forma conmutativa, asociativa e idempotente.

        Los atomos nuevos se incorporan por ID. Para un atomo ya conocido solo
        puede avanzar ``deleted`` de False a True; la posicion nunca se modifica.
        """
        changed = False

        for raw in incoming:
            if "position" not in raw:
                raise ValueError(
                    "Estado CRDT antiguo detectado. Elimina el volumen crdt_data "
                    "en ambos nodos antes de usar esta version."
                )

            atom_id = raw["id"]
            if atom_id not in self.atoms:
                atom = Atom(
                    id=atom_id,
                    position=list(raw["position"]),
                    block_id=raw["block_id"],
                    offset=int(raw["offset"]),
                    value=raw["value"],
                    deleted=bool(raw.get("deleted", False)),
                )
                self.atoms[atom_id] = atom
                changed = True
            elif raw.get("deleted", False) and not self.atoms[atom_id].deleted:
                self.atoms[atom_id].deleted = True
                changed = True

            # Recupera el contador al cargar operaciones antiguas de este nodo.
            block_id = raw.get("block_id", "")
            if block_id.startswith(self.node_id + ":"):
                try:
                    self.counter = max(
                        self.counter, int(block_id.rsplit(":", 1)[1])
                    )
                except ValueError:
                    pass

        return changed

    def state(self) -> List[dict]:
        """Estado JSON serializable que se persiste y se envia a los peers."""
        return [asdict(atom) for atom in self.atoms.values()]

    def apply_text_change(self, old_text: str, new_text: str) -> None:
        """Convierte el cambio del textarea en borrados e inserciones CRDT.

        Conserva el prefijo y el sufijo comunes. La parte central eliminada se
        transforma en tombstones y la parte central nueva se inserta entre las
        posiciones inmutables de sus vecinos.
        """
        visible = self.visible_atoms()
        actual = "".join(atom.value for atom in visible)

        # Si el navegador envio una version atrasada, la estructura local es la
        # fuente de verdad para calcular los atomos afectados.
        if old_text != actual:
            old_text = actual

        prefix = 0
        while (
            prefix < len(old_text)
            and prefix < len(new_text)
            and old_text[prefix] == new_text[prefix]
        ):
            prefix += 1

        suffix = 0
        while (
            suffix < len(old_text) - prefix
            and suffix < len(new_text) - prefix
            and old_text[-1 - suffix] == new_text[-1 - suffix]
        ):
            suffix += 1

        old_end = len(old_text) - suffix
        new_end = len(new_text) - suffix

        # Los caracteres sustituidos o borrados se convierten en tombstones.
        for atom in visible[prefix:old_end]:
            self.delete(atom.id)

        # Los vecinos visibles conservados delimitan la posicion nueva.
        left_position = visible[prefix - 1].position if prefix > 0 else None
        right_position = (
            visible[old_end].position
            if suffix > 0 and old_end < len(visible)
            else None
        )

        self._insert_block(
            left_position,
            right_position,
            new_text[prefix:new_end],
        )

RGA = SequenceCRDT
