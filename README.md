# Editor colaborativo CRDT

Demostración académica de un documento replicado con **dos nodos independientes**, desarrollada con Python, FastAPI, HTTPX, WebSockets y Docker Compose.

Cada nodo mantiene su propia réplica persistente. Durante una partición puede aceptar cambios locales y, al recuperar la comunicación, intercambia su estado con el otro nodo y ejecuta un `merge` determinista.

## Modelo implementado

El proyecto utiliza un **Sequence CRDT basado en posiciones lógicas inmutables**, sincronizado como **state-based CRDT**.

Cada carácter se representa mediante un átomo:

```text
Atom(id, position, block_id, offset, value, deleted)
```

- `id`: identidad global del carácter.
- `position`: posición lógica inmutable dentro del documento.
- `block_id`: identifica la edición que creó un bloque de texto.
- `offset`: orden del carácter dentro del bloque.
- `value`: carácter almacenado.
- `deleted`: tombstone; si es `true`, no se muestra pero sigue formando parte del estado.

## Requisitos

- Docker Desktop.
- Docker Compose.
- Dos máquinas o dos entornos capaces de comunicarse por HTTP.
- El mismo `PEER_TOKEN` en ambos nodos.

## Configuración

El repositorio contiene los mismos archivos de aplicación para ambos nodos. Los archivos `.env` están separados en las carpetas `crdt-local/` y `crdt-servidor/`.

Para levantar el **Nodo A local**, copiar el `.env` de `crdt-local/` a la carpeta principal del proyecto, junto a `compose.yaml`:

```powershell
Copy-Item .\crdt-local\.env .\.env
```

Para preparar el **Nodo B**, utilizar el `.env` de `crdt-servidor/` al desplegar el proyecto en el servidor.

> **Nota para reproducir la demo:** los valores actuales de `PEERS` corresponden al entorno utilizado durante la demostración. Si se desea ejecutar el proyecto en otras máquinas o servidores, hay que modificar `PEERS` en ambos `.env` para que cada nodo apunte a una dirección HTTP accesible del otro. Ambos nodos deben utilizar el mismo `PEER_TOKEN`.

### Configuración utilizada en la demostración

#### Nodo A, notebook local

```env
NODE_ID=A
NODE_NAME=Nodo local
PEERS=http://sd-2026.tailb9d8eb.ts.net:18010
PEER_TOKEN=Clave segura para ambos
SEED_TEXT=hola mundo!
HOST_PORT=8000
```

#### Nodo B, servidor de prácticas

```env
NODE_ID=B
NODE_NAME=Nodo servidor
PEERS=http://gustavo-ntb:8000
PEER_TOKEN=Clave segura para ambos
SEED_TEXT=hola mundo!
HOST_PORT=8080
```

## Puesta en marcha

### 1. Levantar el Nodo A local

Desde PowerShell, situarse en la carpeta principal del proyecto, donde se encuentra `compose.yaml`:

```powershell
Copy-Item .\crdt-local\.env .\.env
docker compose up -d --build
```

`docker compose up -d --build` construye la imagen antes de iniciar el servicio y lo deja ejecutándose en segundo plano. 

Comprobar el contenedor:

```powershell
docker compose ps
```

Comprobar la API local:

```powershell
Invoke-RestMethod "http://localhost:8000/health"
```

La interfaz del Nodo A queda disponible en:

```text
http://localhost:8000
```

### 2. Preparar y subir el Nodo B al servidor

Los comandos siguientes reproducen el despliegue utilizado en la demostración. Si se utiliza otro servidor, reemplazar el usuario, host, puerto SSH y rutas por los correspondientes.

Primero se recomienda crear una copia limpia del proyecto para el servidor. Debe contener los mismos archivos de aplicación que el nodo local, pero utilizando el `.env` de `crdt-servidor/`.

Desde PowerShell, situado en la carpeta que contiene el proyecto, copiar la carpeta al servidor mediante `scp`:

```powershell
scp -P <PUERTO_SSH> -r ".\crdt-servidor" <USUARIO>@<SERVIDOR>:~/
```

`scp` permite copiar archivos entre equipos a través de una conexión SSH; `-P` selecciona el puerto remoto y `-r` copia una carpeta de forma recursiva.

> Si la carpeta `crdt-servidor/` del repositorio contiene únicamente el `.env`, antes de ejecutar este comando hay que preparar una carpeta de despliegue con los archivos comunes del proyecto (`app/`, `Dockerfile`, `compose.yaml` y `requirements.txt`) y colocar dentro el `.env` de `crdt-servidor/`. El código de ambos nodos es el mismo; cambia la configuración del `.env`.

### 3. Levantar el Nodo B en el servidor

Conectarse al servidor por SSH:

```powershell
ssh -p <PUERTO_SSH> <USUARIO>@<SERVIDOR>
```

Una vez conectado:

```bash
cd ~/crdt-servidor
ls -la
docker compose up -d --build
```

Comprobar que el servicio está levantado:

```bash
docker compose ps
```

Comprobar la API desde el propio servidor:

```bash
curl http://localhost:8080/health
```

En la infraestructura utilizada para la demostración, la aplicación del servidor se publica externamente mediante una redirección hacia el puerto configurado en el host. En otros entornos, la URL externa dependerá de la configuración de red del servidor.

### 4. Comprobar la comunicación entre los dos nodos

Desde la notebook, comprobar el Nodo A:

```powershell
(Invoke-RestMethod "http://localhost:8000/api/state").text
```

Y comprobar el Nodo B utilizando su dirección accesible:

```powershell
(Invoke-RestMethod "http://<DIRECCION_NODO_B>:<PUERTO>/api/state").text
```

Cuando ambos nodos pueden alcanzar las direcciones configuradas en `PEERS`, la sincronización se realiza automáticamente.

### Acceso utilizado durante la demostración

- Nodo A: `http://localhost:8000`
- Nodo B: `http://sd-2026.tailb9d8eb.ts.net:18010`

## Funcionamiento

### Edición local

El navegador envía `old_text` y `new_text` a `POST /api/edit`. El backend transforma la diferencia en tombstones e inserciones CRDT, guarda el estado y actualiza los navegadores conectados.

### Sincronización

Cada segundo, `sync_loop()` ejecuta `sync_once()`:

1. Toma el estado local.
2. Lo envía a `POST /api/merge` del peer.
3. El peer incorpora átomos desconocidos y tombstones.
4. El peer devuelve su estado combinado.
5. El nodo iniciador también integra la respuesta.
6. Si hubo cambios, se persisten y se notifican por WebSocket.

### Partición simulada

El botón **Simular partición** cambia `sync_enabled` a `false`.

Mientras está desactivado:

- el nodo no ejecuta sincronizaciones salientes;
- rechaza `/api/merge` con HTTP 503;
- continúa aceptando ediciones locales;
- continúa persistiendo su réplica.

Al pulsar **Restablecer conexión**, vuelve la sincronización automática y ambos nodos convergen.

### Actualización del navegador

La interfaz utiliza:

- WebSocket para recibir cambios inmediatamente;
- reconexión automática del WebSocket;
- `GET /api/state` cada dos segundos como mecanismo de reconciliación si se perdió una notificación durante un corte.

## Persistencia

El estado se guarda en:

```text
/data/state.json
```

La ruta `/data` está montada sobre el volumen nombrado `crdt_data`.

Recrear el contenedor conservando el documento:

```bash
docker compose down
docker compose up -d --build
```

Reiniciar completamente el nodo y borrar el documento persistido:

```bash
docker compose down -v
docker compose up -d --build
```

> `down -v` elimina el volumen. Solo usar cuando se quiera empezar desde `SEED_TEXT`.

## Endpoints

| Método | Ruta | Descripción |
|---|---|---|
| GET | `/` | Interfaz web |
| GET | `/health` | Estado básico del nodo |
| GET | `/api/state` | Snapshot actual |
| POST | `/api/edit` | Aplicar edición local |
| POST | `/api/merge` | Integrar estado remoto |
| POST | `/api/sync/toggle` | Activar/desactivar sincronización |
| POST | `/api/reset` | Reiniciar sólo la réplica local |
| WS | `/ws` | Actualizaciones del navegador |

## Demo recomendada

1. Abrir A y B y comprobar que muestran el mismo texto.
2. Escribir con los nodos conectados.
3. Pulsar **Simular partición** en uno de ellos.
4. Realizar cambios distintos en A y B.
5. Mostrar que ambos nodos siguen disponibles pero divergen.
6. Pulsar **Restablecer conexión**.
7. Esperar el intercambio automático.
8. Verificar que ambos documentos convergen.
9. Borrar caracteres y observar el contador de tombstones.

También puede desconectarse el Wi-Fi del nodo local para provocar una pérdida real de conectividad. El Nodo A seguirá accesible por `localhost`; al volver la red, reintentará la sincronización.

## Comandos de diagnóstico

```bash
docker compose ps
docker compose logs -f crdt-node
docker compose config --quiet
docker compose volumes
```

Consultas de estado desde PowerShell:

```powershell
(Invoke-RestMethod "http://localhost:8000/api/state").text
(Invoke-RestMethod "http://sd-2026.tailb9d8eb.ts.net:18010/api/state").text
```
