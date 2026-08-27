#!/usr/bin/env bash

# Script para ejecutar spoty-scanner con el entorno virtual
set -euo pipefail

# Colores para el output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color
REQUIRED_PYTHON="3.12"

select_python_bin() {
    if command -v python3.12 >/dev/null 2>&1; then
        echo "python3.12"
        return
    fi
    if command -v python3 >/dev/null 2>&1; then
        echo "python3"
        return
    fi
    echo ""
}

python_meets_requirement() {
    local py_bin="$1"
    "$py_bin" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)'
}

python_version_str() {
    local py_bin="$1"
    "$py_bin" -c 'import sys; print("{}.{}.{}".format(*sys.version_info[:3]))'
}

source_dotenv_if_present() {
    if [ -f ".env" ]; then
        echo -e "${BLUE}📋 Cargando credenciales desde .env${NC}"
        set -a
        # shellcheck disable=SC1091
        source .env
        set +a
    else
        echo -e "${YELLOW}⚠️  No se encontró archivo .env${NC}"
        echo -e "${BLUE}Ejecuta ./setup.sh para configurar las credenciales${NC}"
    fi
}

echo -e "${BLUE}🎵 Spoty Scanner - Generador de Playlists de Discografía${NC}"
echo "================================================="

PYTHON_BIN="$(select_python_bin)"
if [ -z "$PYTHON_BIN" ]; then
    echo -e "${RED}❌ No se encontró python3 en el sistema.${NC}"
    exit 1
fi

if ! python_meets_requirement "$PYTHON_BIN"; then
    DETECTED="$(python_version_str "$PYTHON_BIN")"
    echo -e "${RED}❌ Python $DETECTED detectado; este proyecto requiere Python ${REQUIRED_PYTHON}+.${NC}"
    echo -e "${YELLOW}Instala python3.12 y vuelve a ejecutar ./run.sh.${NC}"
    exit 1
fi

source_dotenv_if_present

# Verificar si existe el entorno virtual
if [ ! -x "venv/bin/python" ]; then
    echo -e "${RED}❌ No se encontró el entorno virtual.${NC}"
    echo -e "${BLUE}Creando entorno virtual con $PYTHON_BIN...${NC}"
    "$PYTHON_BIN" -m venv venv
else
    if ! python_meets_requirement "venv/bin/python"; then
        VENV_VERSION="$(python_version_str "venv/bin/python")"
        echo -e "${RED}❌ El entorno virtual actual usa Python $VENV_VERSION.${NC}"
        echo -e "${YELLOW}Recréalo con:${NC} rm -rf venv && $PYTHON_BIN -m venv venv"
        exit 1
    fi
fi

# Activar entorno virtual
echo -e "${BLUE}🔧 Activando entorno virtual...${NC}"
# shellcheck disable=SC1091
source venv/bin/activate

# Instalar dependencias si no están instaladas
echo -e "${BLUE}📦 Verificando dependencias...${NC}"
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

# Verificar credenciales
if [ -z "${BOT_TOKEN:-}" ] || [ -z "${SPOTIFY_CLIENT_ID:-}" ] || [ -z "${SPOTIFY_CLIENT_SECRET:-}" ]; then
    echo -e "${RED}❌ Faltan credenciales requeridas (BOT_TOKEN y/o Spotify API).${NC}"
    echo -e "${YELLOW}Ejecuta './setup.sh' para configurarlas${NC}"
    deactivate
    exit 1
fi

# Ejecutar el programa
echo -e "${GREEN}🚀 Ejecutando Spoty Scanner...${NC}"
echo ""
python main.py

# Desactivar entorno virtual
deactivate
