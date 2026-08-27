#!/usr/bin/env bash

set -euo pipefail

# Script de configuración para Spoty Scanner

echo "🎵 Spoty Scanner - Configuración inicial"
echo "========================================"
echo ""

# Colores
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

upsert_env_var() {
    local file="$1"
    local key="$2"
    local raw_value="$3"
    local escaped_value="${raw_value//\\/\\\\}"
    escaped_value="${escaped_value//\"/\\\"}"

    local tmp
    tmp="$(mktemp)"
    awk -v k="$key" -v v="$escaped_value" '
        BEGIN { updated = 0 }
        $0 ~ "^(export[[:space:]]+)?" k "=" {
            if (!updated) {
                print k "=\"" v "\""
                updated = 1
            }
            next
        }
        { print }
        END {
            if (!updated) {
                print k "=\"" v "\""
            }
        }
    ' "$file" > "$tmp"
    mv "$tmp" "$file"
}

echo -e "${BLUE}Este script te ayudará a configurar las credenciales de Spotify API.${NC}"
echo ""

# Cargar .env existente para detectar configuración previa
if [ -f ".env" ]; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
fi

# Verificar si las variables ya están configuradas
if [ -n "${SPOTIFY_CLIENT_ID:-}" ] && [ -n "${SPOTIFY_CLIENT_SECRET:-}" ]; then
    echo -e "${GREEN}✅ Las credenciales ya están configuradas en el entorno.${NC}"
    echo "CLIENT_ID: $SPOTIFY_CLIENT_ID"
    echo "CLIENT_SECRET: [oculto]"
    echo "REDIRECT_URI: ${SPOTIFY_REDIRECT_URI:-http://localhost:8888/callback}"
    echo ""
    read -p "¿Quieres reconfigurar las credenciales? (y/N): " reconfigure
    if [[ $reconfigure != "y" && $reconfigure != "Y" ]]; then
        echo "Manteniendo credenciales actuales."
        exit 0
    fi
fi

echo -e "${YELLOW}📋 Pasos para obtener credenciales:${NC}"
echo "1. Ve a https://developer.spotify.com/dashboard"
echo "2. Inicia sesión con tu cuenta de Spotify"
echo "3. Crea una nueva aplicación (App)"
echo "4. Ve a Settings de tu aplicación"
echo "5. Copia el Client ID y Client Secret"
echo "6. En 'Redirect URIs' añade: http://localhost:8888/callback"
echo ""

# Pedir Client ID
echo -e "${BLUE}Ingresa tu Client ID de Spotify:${NC}"
read -p "> " client_id

if [ -z "$client_id" ]; then
    echo -e "${RED}❌ Client ID no puede estar vacío.${NC}"
    exit 1
fi

# Pedir Client Secret
echo -e "${BLUE}Ingresa tu Client Secret de Spotify:${NC}"
read -s -p "> " client_secret
echo ""

if [ -z "$client_secret" ]; then
    echo -e "${RED}❌ Client Secret no puede estar vacío.${NC}"
    exit 1
fi

# Redirect URI (por defecto)
redirect_uri="http://localhost:8888/callback"
echo -e "${BLUE}Redirect URI (presiona Enter para usar el predeterminado):${NC}"
echo "Predeterminado: $redirect_uri"
read -p "> " custom_redirect_uri

if [ -n "$custom_redirect_uri" ]; then
    redirect_uri="$custom_redirect_uri"
fi

# Guardar en archivo .env sin sobrescribir otras variables
if [ ! -f ".env" ]; then
    touch .env
fi

backup_file=".env.bak.$(date +%Y%m%d%H%M%S)"
cp .env "$backup_file"

upsert_env_var ".env" "SPOTIFY_CLIENT_ID" "$client_id"
upsert_env_var ".env" "SPOTIFY_CLIENT_SECRET" "$client_secret"
upsert_env_var ".env" "SPOTIFY_REDIRECT_URI" "$redirect_uri"

# Cargar en la sesión actual
export SPOTIFY_CLIENT_ID="$client_id"
export SPOTIFY_CLIENT_SECRET="$client_secret"
export SPOTIFY_REDIRECT_URI="$redirect_uri"

echo ""
echo -e "${GREEN}✅ Credenciales configuradas correctamente!${NC}"
echo -e "${BLUE}🗂️ Backup del .env previo: ${backup_file}${NC}"
echo ""
echo -e "${BLUE}Para usar estas credenciales en futuras sesiones, ejecuta:${NC}"
echo "source .env"
echo ""
echo -e "${BLUE}O añade las siguientes líneas a tu ~/.zshrc o ~/.bashrc:${NC}"
echo "export SPOTIFY_CLIENT_ID=\"$client_id\""
echo "export SPOTIFY_CLIENT_SECRET=\"[tu_client_secret]\""
echo "export SPOTIFY_REDIRECT_URI=\"$redirect_uri\""
echo ""

echo -e "${GREEN}🚀 ¡Ahora puedes ejecutar el programa con: ./run.sh${NC}"
