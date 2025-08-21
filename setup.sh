#!/bin/bash

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

echo -e "${BLUE}Este script te ayudará a configurar las credenciales de Spotify API.${NC}"
echo ""

# Verificar si las variables ya están configuradas
if [ ! -z "$SPOTIFY_CLIENT_ID" ] && [ ! -z "$SPOTIFY_CLIENT_SECRET" ]; then
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

if [ ! -z "$custom_redirect_uri" ]; then
    redirect_uri="$custom_redirect_uri"
fi

# Guardar en archivo .env
echo "# Credenciales de Spotify API" > .env
echo "export SPOTIFY_CLIENT_ID=\"$client_id\"" >> .env
echo "export SPOTIFY_CLIENT_SECRET=\"$client_secret\"" >> .env
echo "export SPOTIFY_REDIRECT_URI=\"$redirect_uri\"" >> .env

# Cargar las variables en la sesión actual
source .env

echo ""
echo -e "${GREEN}✅ Credenciales configuradas correctamente!${NC}"
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
