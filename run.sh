#!/bin/bash

# Script para ejecutar spoty-scanner con el entorno virtual

# Colores para el output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${BLUE}🎵 Spoty Scanner - Generador de Playlists de Discografía${NC}"
echo "================================================="

# Cargar variables de entorno si existe .env
if [ -f ".env" ]; then
    echo -e "${BLUE}📋 Cargando credenciales desde .env${NC}"
    source .env
else
    echo -e "${YELLOW}⚠️  No se encontró archivo .env${NC}"
    echo -e "${BLUE}Ejecuta ./setup.sh para configurar las credenciales${NC}"
fi

# Verificar si existe el entorno virtual
if [ ! -d "venv" ]; then
    echo -e "${RED}❌ No se encontró el entorno virtual.${NC}"
    echo -e "${BLUE}Creando entorno virtual...${NC}"
    python3 -m venv venv
fi

# Activar entorno virtual
echo -e "${BLUE}🔧 Activando entorno virtual...${NC}"
source venv/bin/activate

# Instalar dependencias si no están instaladas
echo -e "${BLUE}📦 Verificando dependencias...${NC}"
pip install -r requirements.txt > /dev/null 2>&1

# Verificar credenciales
if [ -z "$SPOTIFY_CLIENT_ID" ] || [ -z "$SPOTIFY_CLIENT_SECRET" ]; then
    echo -e "${RED}❌ Faltan credenciales de Spotify API${NC}"
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
