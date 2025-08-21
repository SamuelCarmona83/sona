import os
import spotipy
from spotipy.oauth2 import SpotifyOAuth
import time
from collections import defaultdict

class SpotifyDiscographyPlaylist:
    def __init__(self, client_id=None, client_secret=None, redirect_uri=None):
        """
        Inicializa la conexión con Spotify API
        
        Args:
            client_id: Tu Client ID de Spotify (opcional si está en variables de entorno)
            client_secret: Tu Client Secret de Spotify (opcional si está en variables de entorno)
            redirect_uri: URI de redirección (opcional, por defecto: http://localhost:8888/callback)
        """
        # Usar variables de entorno si no se proporcionan credenciales
        self.client_id = client_id or os.getenv('SPOTIFY_CLIENT_ID')
        self.client_secret = client_secret or os.getenv('SPOTIFY_CLIENT_SECRET')
        self.redirect_uri = redirect_uri or os.getenv('SPOTIFY_REDIRECT_URI', 'http://localhost:8888/callback')
        
        if not self.client_id or not self.client_secret:
            raise ValueError("""
❌ Faltan credenciales de Spotify!

Por favor, configura tus credenciales de una de estas formas:

1. Variables de entorno:
   export SPOTIFY_CLIENT_ID="tu_client_id"
   export SPOTIFY_CLIENT_SECRET="tu_client_secret"
   export SPOTIFY_REDIRECT_URI="http://localhost:8888/callback"

2. O pásalas directamente al constructor:
   SpotifyDiscographyPlaylist(client_id="...", client_secret="...")

🔗 Obtén tus credenciales en: https://developer.spotify.com/dashboard
            """)
        
        scope = "playlist-modify-public playlist-modify-private user-library-read"
        
        self.sp = spotipy.Spotify(auth_manager=SpotifyOAuth(
            client_id=self.client_id,
            client_secret=self.client_secret,
            redirect_uri=self.redirect_uri,
            scope=scope
        ))
        
    def search_artist(self, artist_name):
        """Busca un artista por nombre y devuelve información básica"""
        results = self.sp.search(q=f'artist:{artist_name}', type='artist', limit=1)
        
        if results['artists']['items']:
            artist = results['artists']['items'][0]
            print(f"Artista encontrado: {artist['name']}")
            print(f"ID: {artist['id']}")
            print(f"Seguidores: {artist['followers']['total']:,}")
            print(f"Géneros: {', '.join(artist['genres'])}")
            return artist
        else:
            print(f"No se encontró el artista: {artist_name}")
            return None
    
    def get_artist_by_id(self, artist_id):
        """Obtiene información de un artista por su ID"""
        try:
            artist = self.sp.artist(artist_id)
            print(f"Artista encontrado: {artist['name']}")
            print(f"ID: {artist['id']}")
            print(f"Seguidores: {artist['followers']['total']:,}")
            print(f"Géneros: {', '.join(artist['genres'])}")
            return artist
        except Exception as e:
            print(f"Error obteniendo artista con ID {artist_id}: {str(e)}")
            return None
    
    def get_artist_albums(self, artist_id, include_groups=['album', 'single', 'compilation']):
        """
        Obtiene todos los álbumes de un artista
        
        Args:
            artist_id: ID del artista en Spotify
            include_groups: Tipos de releases a incluir
        """
        albums = []
        results = self.sp.artist_albums(
            artist_id, 
            album_type=','.join(include_groups), 
            limit=50
        )
        
        albums.extend(results['items'])
        
        # Paginación para obtener todos los álbumes
        while results['next']:
            results = self.sp.next(results)
            albums.extend(results['items'])
        
        # Eliminar duplicados por nombre (diferentes mercados)
        unique_albums = {}
        for album in albums:
            album_key = (album['name'].lower(), album['release_date'][:4])  # nombre + año
            if album_key not in unique_albums:
                unique_albums[album_key] = album
        
        sorted_albums = sorted(
            unique_albums.values(), 
            key=lambda x: x['release_date']
        )
        
        print(f"\nEncontrados {len(sorted_albums)} álbumes únicos:")
        for album in sorted_albums:
            print(f"- {album['name']} ({album['release_date'][:4]}) - {album['album_type']}")
        
        return sorted_albums
    
    def get_album_tracks(self, album_id):
        """Obtiene todas las canciones de un álbum"""
        tracks = []
        results = self.sp.album_tracks(album_id, limit=50)
        
        tracks.extend(results['items'])
        
        # Paginación
        while results['next']:
            results = self.sp.next(results)
            tracks.extend(results['items'])
            
        return tracks
    
    def create_discography_playlist(self, artist_name=None, artist_id=None, playlist_name=None, 
                                  include_groups=['album', 'single'], 
                                  remove_duplicates=True):
        """
        Crea una playlist con toda la discografía de un artista
        
        Args:
            artist_name: Nombre del artista
            artist_id: ID del artista (alternativa a artist_name)
            playlist_name: Nombre de la playlist (opcional)
            include_groups: Tipos de releases a incluir
            remove_duplicates: Si eliminar canciones duplicadas
        """
        # Buscar artista
        if artist_id:
            artist = self.get_artist_by_id(artist_id)
        elif artist_name:
            artist = self.search_artist(artist_name)
        else:
            print("Debes proporcionar artist_name o artist_id")
            return None
            
        if not artist:
            return None
        
        # Obtener álbumes
        albums = self.get_artist_albums(artist['id'], include_groups)
        
        # Recopilar todas las canciones
        all_tracks = []
        track_names = set() if remove_duplicates else None
        
        print(f"\nRecopilando canciones...")
        
        for i, album in enumerate(albums, 1):
            print(f"Procesando álbum {i}/{len(albums)}: {album['name']}")
            
            try:
                tracks = self.get_album_tracks(album['id'])
                
                for track in tracks:
                    # Verificar si es del artista principal (no colaboraciones)
                    is_main_artist = any(
                        artist['id'] == track_artist['id'] 
                        for track_artist in track['artists']
                    )
                    
                    if is_main_artist:
                        if remove_duplicates:
                            track_key = track['name'].lower()
                            if track_key not in track_names:
                                track_names.add(track_key)
                                all_tracks.append(track['id'])
                        else:
                            all_tracks.append(track['id'])
                
                # Pausa para evitar rate limiting
                time.sleep(0.1)
                
            except Exception as e:
                print(f"Error procesando álbum {album['name']}: {str(e)}")
                continue
        
        print(f"\nTotal de canciones recopiladas: {len(all_tracks)}")
        
        # Verificar límite de Spotify (10,000 canciones por playlist)
        if len(all_tracks) > 10000:
            print(f"⚠️  ADVERTENCIA: {len(all_tracks)} canciones excede el límite de 10,000 de Spotify")
            print("Se tomarán las primeras 10,000 canciones")
            all_tracks = all_tracks[:10000]
        
        # Crear playlist
        if not playlist_name:
            playlist_name = f"{artist['name']} - Discografía Completa"
        
        user_id = self.sp.current_user()['id']
        
        playlist = self.sp.user_playlist_create(
            user_id, 
            playlist_name,
            description=f"Discografía completa de {artist['name']} - Generada automáticamente"
        )
        
        print(f"\nPlaylist creada: {playlist_name}")
        print(f"ID de playlist: {playlist['id']}")
        
        # Añadir canciones en lotes de 100 (límite de la API)
        batch_size = 100
        for i in range(0, len(all_tracks), batch_size):
            batch = all_tracks[i:i + batch_size]
            try:
                self.sp.playlist_add_items(playlist['id'], batch)
                print(f"Añadidas canciones {i+1} - {min(i + batch_size, len(all_tracks))}")
                time.sleep(0.1)  # Pausa para evitar rate limiting
            except Exception as e:
                print(f"Error añadiendo lote de canciones: {str(e)}")
        
        print(f"\n✅ ¡Playlist completada!")
        print(f"URL: {playlist['external_urls']['spotify']}")
        
        return playlist

# Ejemplo de uso
def main():
    try:
        # Crear instancia (usará variables de entorno si están configuradas)
        spotify_tool = SpotifyDiscographyPlaylist()
        
        # ID del artista específico de la URL proporcionada
        # https://open.spotify.com/artist/3wtMPMvPtiFylbnNXF6CAj/discography/all
        artist_id = "3wtMPMvPtiFylbnNXF6CAj"
        
        print("🎵 Spoty Scanner - Generador de Playlist de Discografía")
        print("=====================================================")
        print()
        print("¿Qué incluir en la playlist?")
        print("1. Solo álbumes")
        print("2. Álbumes + Singles")
        print("3. Todo (álbumes + singles + compilaciones)")
        print()
        
        choice = input("Selecciona una opción (1-3): ").strip()
        
        include_groups_map = {
            '1': ['album'],
            '2': ['album', 'single'],
            '3': ['album', 'single', 'compilation']
        }
        
        include_groups = include_groups_map.get(choice, ['album', 'single'])
        
        print(f"\n🔍 Procesando artista con ID: {artist_id}")
        print("=" * 50)
        
        # Crear playlist usando el artist_id específico
        spotify_tool.create_discography_playlist(
            artist_id=artist_id,
            include_groups=include_groups
        )
        
    except ValueError as e:
        print(e)
        print("\n📋 Pasos para configurar las credenciales:")
        print("1. Ve a https://developer.spotify.com/dashboard")
        print("2. Crea una nueva aplicación o usa una existente")
        print("3. Obtén tu Client ID y Client Secret")
        print("4. Configura el Redirect URI como: http://localhost:8888/callback")
        print("5. Exporta las variables de entorno:")
        print('   export SPOTIFY_CLIENT_ID="tu_client_id_aqui"')
        print('   export SPOTIFY_CLIENT_SECRET="tu_client_secret_aqui"')
        print("6. Vuelve a ejecutar el programa")
        
    except Exception as e:
        print(f"❌ Error inesperado: {str(e)}")

if __name__ == "__main__":
    main()