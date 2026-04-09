# 🎵 Spoty Scanner

Un generador automático de playlists con la discografía completa de artistas de Spotify.

## 🎯 Características

- ✅ Crea playlists con toda la discografía de un artista
- 🎛️ Configurable: álbumes, singles, compilaciones
- 🔄 Elimina duplicados automáticamente
- 📊 Maneja paginación de la API de Spotify
- 🚫 Respeta los límites de rate limiting
- 🔒 Gestión segura de credenciales

## 🚀 Inicio Rápido

### 1. Configurar credenciales de Spotify

```bash
./setup.sh
```

Este script te guiará para:
- Obtener credenciales en https://developer.spotify.com/dashboard
- Configurar las variables de entorno necesarias

### 2. Ejecutar el programa

```bash
./run.sh
```

Este script:
- Crea y activa el entorno virtual automáticamente
- Instala las dependencias necesarias
- Ejecuta el programa

## 🧪 POC: setlist.fm URL -> Spotify Playlist

Esta prueba de concepto está encapsulada en `poc_setlistfm.py` y **no reemplaza** el flujo actual de `main.py`.

### Variables necesarias

Además de las credenciales de Spotify, agrega tu token de setlist.fm:

```bash
export SETLISTFM_TOKEN="tu_token_setlistfm"
```

También funciona si está en `.env` como:

```bash
SETLISTFM_TOKEN="tu_token_setlistfm"
```

### Ejecutar la POC

```bash
source .env
python poc_setlistfm.py
```

Luego pega una URL de setlist.fm, por ejemplo:

```text
https://www.setlist.fm/setlist/men-i-trust/2026/hipodromo-de-san-isidro-alternative-stage-san-isidro-argentina-6b410e0a.html
```

La POC:
- Lee la setlist desde la API oficial de setlist.fm
- Busca cada canción en Spotify manteniendo el orden
- Crea una playlist nueva y muestra resumen de canciones no encontradas

### Nota de uso de API

- El uso de la API de setlist.fm es para proyectos no comerciales según sus términos.
- Muestra atribución de fuente al final de la ejecución.

## 📋 Configuración Manual

Si prefieres configurar manualmente:

### 1. Crear entorno virtual
```bash
python3 -m venv venv
source venv/bin/activate
```

### 2. Instalar dependencias
```bash
pip install -r requirements.txt
```

### 3. Configurar variables de entorno
```bash
export SPOTIFY_CLIENT_ID="tu_client_id"
export SPOTIFY_CLIENT_SECRET="tu_client_secret"
export SPOTIFY_REDIRECT_URI="http://localhost:8888/callback"
```

### 4. Ejecutar
```bash
python main.py
```

## 🎨 Configuración del Artista

El programa está preconfigurado para procesar el artista con ID: `3wtMPMvPtiFylbnNXF6CAj`

Para cambiar el artista, edita la variable `artist_id` en `main.py`:

```python
# ID del artista específico
artist_id = "TU_ARTIST_ID_AQUI"
```

Puedes obtener el ID del artista desde la URL de Spotify:
- URL: `https://open.spotify.com/artist/3wtMPMvPtiFylbnNXF6CAj`
- ID: `3wtMPMvPtiFylbnNXF6CAj`

## 🔧 Opciones de Configuración

El programa te permite elegir qué incluir:

1. **Solo álbumes** - Solo álbumes de estudio
2. **Álbumes + Singles** - Álbumes y singles (recomendado)
3. **Todo** - Álbumes, singles y compilaciones

## 📁 Estructura del Proyecto

```
spoty-scanner/
├── main.py           # Programa principal
├── requirements.txt  # Dependencias de Python
├── setup.sh         # Script de configuración
├── run.sh           # Script de ejecución
└── README.md        # Esta documentación
```

## 🔐 Obtener Credenciales de Spotify

1. Ve a [Spotify Developer Dashboard](https://developer.spotify.com/dashboard)
2. Inicia sesión con tu cuenta de Spotify
3. Crea una nueva aplicación
4. Ve a Settings de tu aplicación
5. Copia el **Client ID** y **Client Secret**
6. En **Redirect URIs** añade: `http://localhost:8888/callback`

## ⚠️ Limitaciones

- **Límite de playlist**: Spotify permite máximo 10,000 canciones por playlist
- **Rate limiting**: El programa incluye pausas para respetar los límites de la API
- **Autenticación**: La primera vez se abrirá un navegador para autorizar la aplicación

## 🐛 Solución de Problemas

### Error de credenciales
```
❌ Faltan credenciales de Spotify!
```
**Solución**: Ejecuta `./setup.sh` para configurar las credenciales.

### Error de Redirect URI
```
INVALID_CLIENT: Invalid redirect URI
```
**Solución**: Verifica que hayas añadido `http://localhost:8888/callback` en tu app de Spotify.

### Error de permisos
```
Insufficient client scope
```
**Solución**: El programa solicita automáticamente los permisos necesarios en la primera ejecución.

## 📝 Licencia

Este proyecto es de código abierto y está disponible bajo la licencia MIT.

## 🤝 Contribuciones

Las contribuciones son bienvenidas. Por favor:

1. Fork el proyecto
2. Crea una rama para tu feature
3. Commit tus cambios
4. Push a la rama
5. Abre un Pull Request

---

**¡Disfruta creando tus playlists de discografía completa! 🎶**
