from flask import Flask, request, jsonify
import yt_dlp
import re
import json

import asyncio
import os
import subprocess
import threading
import tempfile
import base64
import shutil

import discord
from discord.ext import commands

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")
PLAYLISTS_DIR = os.path.join(BASE_DIR, "playlists")
_local_ffmpeg = os.path.join(BASE_DIR, "ffmpeg", "bin", "ffmpeg.exe")
FFMPEG_PATH = os.getenv("FFMPEG_PATH") or (_local_ffmpeg if os.path.exists(_local_ffmpeg) else "ffmpeg")
MAX_MP3_BYTES = 100 * 1024 * 1024
MP3_BITRATE = "192k"
MP3_SAMPLE_RATE = "44100"
MP3_CHANNELS = "2"

# Ensure playlists directory exists
os.makedirs(PLAYLISTS_DIR, exist_ok=True)

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
YT_COOKIES_FILE = os.getenv("YT_COOKIES_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt"))  # Path to cookies.txt file
YT_COOKIES_BROWSER = os.getenv("YT_COOKIES_BROWSER")  # Browser name (chrome, firefox, etc)
YT_COOKIES_BASE64 = os.getenv("YT_COOKIES_BASE64")  # Cookies in base64 format (for Render)
YT_PO_TOKEN = os.getenv("YT_PO_TOKEN")  # PO Token for YouTube (experimental)
# Default JS runtime: Deno on Windows, Node.js on Linux/Render
_default_runtime = "deno" if os.name == "nt" else "node"
YT_JS_RUNTIME = os.getenv("YT_JS_RUNTIME", _default_runtime)  # JS runtime for EJS (deno/node/bun/quickjs)
YT_EJS_REMOTE = os.getenv("YT_EJS_REMOTE", "ejs:npm")  # EJS scripts source
YT_JS_RUNTIME_PATH = os.getenv("YT_JS_RUNTIME_PATH")  # Optional explicit path to JS runtime

def find_js_runtime_path(runtime_name):
    """
    Procura por JS runtime (deno, node, etc) em locations comuns do Windows e Linux.
    Se o path foi explicitamente setado via env var YT_JS_RUNTIME_PATH, usa aquele.
    """
    # If explicitly set, use it
    if YT_JS_RUNTIME_PATH:
        if os.path.exists(YT_JS_RUNTIME_PATH):
            print(f"[DEBUG] ✅ JS runtime encontrado (env var): {YT_JS_RUNTIME_PATH}")
            return YT_JS_RUNTIME_PATH
        else:
            print(f"[DEBUG] ⚠️ Env var YT_JS_RUNTIME_PATH setada mas arquivo não existe: {YT_JS_RUNTIME_PATH}")
    
    # Try PATH first
    path_result = shutil.which(runtime_name)
    if path_result:
        print(f"[DEBUG] ✅ JS runtime encontrado no PATH: {path_result}")
        return path_result
    
    # Windows common locations
    if os.name == "nt":
        common_paths = [
            # Deno - WinGet
            os.path.expanduser("~") + r"\AppData\Local\Microsoft\WinGet\Packages\DenoLand.Deno_Microsoft.Winget.Source_8wekyb3d8bbwe\deno.exe",
            # Deno - Chocolatey
            r"C:\ProgramData\chocolatey\bin\deno.exe",
            # Node.js - Program Files
            r"C:\Program Files\nodejs\node.exe",
            # Node.js - Program Files (x86)
            r"C:\Program Files (x86)\nodejs\node.exe",
            # Deno - Manual install
            os.path.expanduser("~") + r"\scoop\shims\deno.exe",
        ]
        
        for path in common_paths:
            if os.path.exists(path):
                print(f"[DEBUG] ✅ JS runtime encontrado (common path): {path}")
                return path
    
    # Linux/Render locations
    else:
        common_paths = [
            "/opt/deno/bin/deno",
            "/usr/local/bin/deno",
            "/usr/bin/deno",
            "/opt/node/bin/node",
            "/usr/local/bin/node",
            "/usr/bin/node",
        ]
        
        for path in common_paths:
            if os.path.exists(path):
                print(f"[DEBUG] ✅ JS runtime encontrado (common path): {path}")
                return path
    
    print(f"[DEBUG] ⚠️ JS runtime '{runtime_name}' não foi encontrado em nenhuma location comum")
    print(f"[DEBUG] Sugestão: SetE variável de ambiente YT_JS_RUNTIME_PATH com o path completo")
    return None

def validate_cookies_file(file_path):
    """Valida se arquivo de cookies tem header correto (Netscape format)"""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            first_line = f.readline().strip()
        
        valid_headers = ['# HTTP Cookie File', '# Netscape HTTP Cookie File']
        if not any(first_line.startswith(h) for h in valid_headers):
            print(f"[DEBUG] ⚠️ Header inválido de cookies. Esperado: {valid_headers}, Encontrado: {first_line}")
            return False
        return True
    except Exception as e:
        print(f"[DEBUG] Erro ao validar arquivo de cookies: {str(e)}")
        return False

def get_ydl_opts(use_cookies=False):
    """Build yt-dlp options with optional cookie support"""
    js_runtime_path = find_js_runtime_path(YT_JS_RUNTIME)
    
    opts = {
        'quiet': True,
        'no_warnings': True,
        'js_runtimes': {YT_JS_RUNTIME: {'path': js_runtime_path}} if js_runtime_path else {YT_JS_RUNTIME: {}},
        'remote_components': [YT_EJS_REMOTE],
        'extractor_args': {
            'youtube': {
                'lang': ['pt', 'en'],
                'player_client': ['web'],
            }
        }
    }
    
    # Add PO Token if available (experimental)
    if YT_PO_TOKEN:
        try:
            opts['extractor_args']['youtube']['po_token'] = [YT_PO_TOKEN]
            print(f"[DEBUG] PO Token adicionado (experimental)")
        except:
            pass
    
    # Add cookies only if explicitly requested
    if use_cookies:
        # First try base64 encoded cookies (for Render)
        if YT_COOKIES_BASE64:
            try:
                cookies_decoded = base64.b64decode(YT_COOKIES_BASE64).decode('utf-8')
                temp_cookies_file = os.path.join(DOWNLOAD_DIR, '.temp_cookies.txt')
                # Use newline='\n' to ensure Unix line endings on all platforms (Linux compatibility)
                with open(temp_cookies_file, 'w', encoding='utf-8', newline='\n') as f:
                    f.write(cookies_decoded)
                
                # Validate cookies file
                if validate_cookies_file(temp_cookies_file):
                    opts['cookiefile'] = temp_cookies_file
                    print(f"[DEBUG] Usando cookies de base64")
                    return opts
                else:
                    print(f"[DEBUG] ⚠️ Cookies base64 inválido - pulando")
            except Exception as e:
                print(f"[DEBUG] Erro ao decodificar cookies base64: {str(e)}")
        
        # Try file cookies
        if YT_COOKIES_FILE and os.path.exists(YT_COOKIES_FILE):
            file_size = os.path.getsize(YT_COOKIES_FILE)
            if file_size > 50:  # File has real content (not just header)
                # Validate cookies file
                if validate_cookies_file(YT_COOKIES_FILE):
                    opts['cookiefile'] = YT_COOKIES_FILE
                    print(f"[DEBUG] Usando cookies do arquivo ({file_size} bytes)")
                    return opts
                else:
                    print(f"[DEBUG] ⚠️ Arquivo de cookies com header inválido")
            else:
                print(f"[DEBUG] Arquivo de cookies vazio ou apenas header")
        
        # Try browser cookies
        if YT_COOKIES_BROWSER:
            opts['cookiesfrombrowser'] = (YT_COOKIES_BROWSER,)
            print(f"[DEBUG] Usando cookies do navegador: {YT_COOKIES_BROWSER}")
            return opts
    
    return opts

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Queue system: {guild_id: {'current': path, 'queue': [(url, title), ...]}}
music_queues = {}

def get_queue(guild_id):
    """Get or initialize queue for a guild"""
    if guild_id not in music_queues:
        music_queues[guild_id] = {'current': None, 'queue': []}
    return music_queues[guild_id]

def cleanup_file(file_path):
    """Safely delete a music file"""
    if file_path and os.path.exists(file_path):
        try:
            os.remove(file_path)
            print(f"[DEBUG] 🗑️ Arquivo removido: {file_path}")
        except OSError as e:
            print(f"[DEBUG] ⚠️ Erro ao remover arquivo: {e}")

# ============= PLAYLIST MANAGEMENT =============

def get_playlist_path(playlist_name):
    """Get file path for a playlist"""
    safe_name = "".join(c for c in playlist_name if c.isalnum() or c in (' ', '-', '_')).strip()
    return os.path.join(PLAYLISTS_DIR, f"{safe_name}.json")

def criar_playlist(playlist_name):
    """Create a new empty playlist"""
    playlist_path = get_playlist_path(playlist_name)
    
    if os.path.exists(playlist_path):
        return False, "Playlist já existe"
    
    playlist_data = {
        "name": playlist_name,
        "songs": []
    }
    
    try:
        with open(playlist_path, 'w', encoding='utf-8') as f:
            json.dump(playlist_data, f, indent=2, ensure_ascii=False)
        return True, "Playlist criada com sucesso"
    except Exception as e:
        return False, f"Erro ao criar playlist: {str(e)}"

def adicionar_a_playlist(playlist_name, url, title=None):
    """Add a song to a playlist"""
    playlist_path = get_playlist_path(playlist_name)
    
    if not os.path.exists(playlist_path):
        return False, "Playlist não encontrada"
    
    try:
        with open(playlist_path, 'r', encoding='utf-8') as f:
            playlist_data = json.load(f)
        
        # Check if song already exists
        if any(song['url'] == url for song in playlist_data['songs']):
            return False, "Música já está na playlist"
        
        playlist_data['songs'].append({
            "url": url,
            "title": title or "Sem título"
        })
        
        with open(playlist_path, 'w', encoding='utf-8') as f:
            json.dump(playlist_data, f, indent=2, ensure_ascii=False)
        
        return True, f"Música adicionada! Total: {len(playlist_data['songs'])}"
    except Exception as e:
        return False, f"Erro ao adicionar música: {str(e)}"

def carregar_playlist(playlist_name):
    """Load a playlist and return its songs"""
    playlist_path = get_playlist_path(playlist_name)
    
    if not os.path.exists(playlist_path):
        return None, "Playlist não encontrada"
    
    try:
        with open(playlist_path, 'r', encoding='utf-8') as f:
            playlist_data = json.load(f)
        return playlist_data['songs'], None
    except Exception as e:
        return None, f"Erro ao carregar playlist: {str(e)}"

def apagar_playlist(playlist_name):
    """Delete a playlist"""
    playlist_path = get_playlist_path(playlist_name)
    
    if not os.path.exists(playlist_path):
        return False, "Playlist não encontrada"
    
    try:
        os.remove(playlist_path)
        return True, "Playlist apagada com sucesso"
    except Exception as e:
        return False, f"Erro ao apagar playlist: {str(e)}"

def listar_playlists():
    """List all available playlists"""
    try:
        playlists = []
        for filename in os.listdir(PLAYLISTS_DIR):
            if filename.endswith('.json'):
                filepath = os.path.join(PLAYLISTS_DIR, filename)
                with open(filepath, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    playlists.append({
                        'name': data['name'],
                        'count': len(data['songs'])
                    })
        return playlists, None
    except Exception as e:
        return None, f"Erro ao listar playlists: {str(e)}"

# ============= END PLAYLIST MANAGEMENT =============

@app.route('/download_mp3', methods=['POST'])
def download_audio():
    data = request.get_json()
    url = data.get('url')

    if not url:
        return jsonify({"error": "Parâmetro 'url' ausente."}), 400

    if not is_valid_youtube_url(url):
        return jsonify({"error": "URL do YouTube inválida."}), 400

    success, mp3_path, error = download_mp3(url)

    if success:
        return jsonify({"message": "MP3 baixado com sucesso."}), 200
    else:
        return jsonify({"error": error}), 500

def download_mp3(url):
    try:
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        
        # Generate unique filename
        mp3_file = os.path.join(DOWNLOAD_DIR, f"{os.urandom(8).hex()}.mp3")
        
        print(f"\n[DEBUG] ========== INICIANDO DOWNLOAD ==========")
        print(f"[DEBUG] URL: {url}")
        print(f"[DEBUG] Plataforma: {os.name}")
        
        # List of strategies to try (cookie-only for local testing)
        strategies = [
            {"name": "Com cookies (audio)", "cookies": True, "format": "bestaudio[ext=m4a]/bestaudio/best"},
            {"name": "Com cookies (fallback)", "cookies": True, "format": "worstaudio/worst"},
            {"name": "Com PO Token (se disponível)", "cookies": True, "format": "bestaudio[ext=m4a]/bestaudio/best"} if YT_PO_TOKEN else None,
        ]
        strategies = [s for s in strategies if s is not None]  # Remove None entries
        
        for idx, strategy in enumerate(strategies, 1):
            try:
                print(f"\n[DEBUG] ===== Tentativa {idx}: {strategy['name']} =====")
                
                ydl_opts = get_ydl_opts(use_cookies=strategy['cookies'])
                ydl_opts.update({
                    'format': strategy['format'],
                    'skip_unavailable_fragments': True,
                    'check_formats': False,
                    'quiet': True,
                    'no_warnings': True,
                    'noprogress': True,
                    'socket_timeout': 30,
                    'retries': 2,
                    'fragment_retries': 2,
                    'concurrent_fragment_downloads': 3,
                    'buffersize': 1024 * 64,
                    'http_chunk_size': 1048576,
                    'throttledratelimit': None,
                    'sleep_interval': 0,
                    'max_sleep_interval': 0,
                    'sleep_interval_requests': 0,
                    'sleep_interval_subtitles': 0,
                    'http_headers': {
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                        'Accept-Language': 'en-us,en;q=0.5',
                        'Sec-Fetch-Mode': 'navigate',
                    },
                    'postprocessors': [{
                        'key': 'FFmpegExtractAudio',
                        'preferredcodec': 'mp3',
                        'preferredquality': '192',
                    }],
                    'postprocessor_args': ['-threads', '2'],
                    'outtmpl': mp3_file.replace('.mp3', ''),
                    'ffmpeg_location': os.path.dirname(FFMPEG_PATH) if os.path.dirname(FFMPEG_PATH) else None,
                })
                
                print(f"[DEBUG] FFMPEG_PATH: {FFMPEG_PATH}")
                print(f"[DEBUG] Format: {strategy['format']}")
                
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([url])
                
                if os.path.exists(mp3_file):
                    file_size = os.path.getsize(mp3_file)
                    print(f"[DEBUG] ✅ {strategy['name']}: Sucesso! Tamanho: {file_size} bytes")
                    if file_size > MAX_MP3_BYTES:
                        os.remove(mp3_file)
                        return False, None, "MP3 excede o limite de 100MB."
                    return True, mp3_file, None
            except Exception as e:
                print(f"[DEBUG] ❌ {strategy['name']}: {type(e).__name__}: {str(e)[:100]}")
                continue
        
        print(f"\n[DEBUG] ========== TODAS AS ESTRATÉGIAS FALHARAM ==========\n")
        return False, None, "Não foi possível fazer download do vídeo - YouTube pode estar bloqueando a requisição"

    except Exception as e:
        print(f"[DEBUG] Erro geral: {type(e).__name__}: {str(e)}")
        return False, None, str(e)


def get_video_info(url):
    try:
        ydl_opts = get_ydl_opts(use_cookies=False)
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            video_info = {
                "title": info.get('title'),
                "author": info.get('uploader'),
                "length": info.get('duration'),
                "views": info.get('view_count'),
                "description": info.get('description'),
                "publish_date": info.get('upload_date'),
            }
            return video_info, None
    except Exception as e:
        return None, str(e)

def is_valid_youtube_url(url):
    pattern = r"^(https?://)?(www\.)?(youtube\.com/(watch\?v=|shorts/)[\w-]+(\?\S*)?(&\S*)?|youtu\.be/[\w-]+(\?\S*)?)$"
    return re.match(pattern, url) is not None


@app.route('/video_info', methods=['POST'])
def video_info():
    data = request.get_json()
    url = data.get('url')
    
    if not url:
        return jsonify({"error": "Parâmetro 'url' ausente."}), 400

    if not is_valid_youtube_url(url):
        return jsonify({"error": "URL do YouTube inválida."}), 400
    
    video_info, error_message = get_video_info(url)
    
    if video_info:
        return jsonify(video_info), 200
    else:
        return jsonify({"error": error_message}), 500


@app.route('/available_resolutions', methods=['POST'])
def available_resolutions():
    data = request.get_json()
    url = data.get('url')
    
    if not url:
        return jsonify({"error": "Parâmetro 'url' ausente."}), 400

    if not is_valid_youtube_url(url):
        return jsonify({"error": "URL do YouTube inválida."}), 400
    
    try:
        ydl_opts = get_ydl_opts(use_cookies=False)
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            formats = info.get('formats', [])
            
            resolutions = list(set([
                f.get('height', 0) 
                for f in formats 
                if f.get('height') and f.get('vcodec') != 'none'
            ]))
            
            return jsonify({
                "resolutions": sorted([f"{r}p" for r in resolutions if r > 0])
            }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


async def ensure_voice(ctx):
    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.send("Você precisa estar em um canal de voz.")
        return None

    channel = ctx.author.voice.channel
    if ctx.voice_client:
        if ctx.voice_client.channel != channel:
            await ctx.voice_client.move_to(channel)
    else:
        await channel.connect()
    return ctx.voice_client


def cleanup_downloads_dir():
    """Clean up old files in downloads directory"""
    if not os.path.isdir(DOWNLOAD_DIR):
        return
    for name in os.listdir(DOWNLOAD_DIR):
        path = os.path.join(DOWNLOAD_DIR, name)
        if os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                pass

async def play_next(ctx):
    """Play next song from queue"""
    queue_data = get_queue(ctx.guild.id)
    voice_client = ctx.voice_client
    
    if not voice_client or not voice_client.is_connected():
        # Clean up current file and clear queue
        if queue_data['current']:
            cleanup_file(queue_data['current'])
            queue_data['current'] = None
        queue_data['queue'].clear()
        return
    
    # Clean up previous song
    if queue_data['current']:
        cleanup_file(queue_data['current'])
        queue_data['current'] = None
    
    # Check if there's a next song
    if not queue_data['queue']:
        await ctx.send("🎵 Fila vazia. Desconectando...")
        await voice_client.disconnect()
        return
    
    # Get next song from queue
    url, title = queue_data['queue'].pop(0)
    await ctx.send(f"⏭️ Tocando próxima: **{title}**")
    
    # Download next song
    loop = asyncio.get_running_loop()
    success, mp3_path, error = await loop.run_in_executor(None, download_mp3, url)
    
    if not success:
        await ctx.send(f"❌ Erro ao baixar próxima música: {error}")
        # Try next song in queue
        await play_next(ctx)
        return
    
    queue_data['current'] = mp3_path
    audio = discord.FFmpegPCMAudio(mp3_path, executable=FFMPEG_PATH)
    
    def after_play(err):
        if err:
            print(f"Playback error: {err}")
        # Play next song when this one finishes
        asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop)
    
    voice_client.play(audio, after=after_play)
    
    # Show queue status
    if queue_data['queue']:
        await ctx.send(f"📋 **{len(queue_data['queue'])}** música(s) na fila")


@bot.event
async def on_ready():
    print(f"Bot conectado como {bot.user}")


@bot.command(name="ajuda")
async def ajuda(ctx):
    help_message = (
        "📜 **Comandos disponíveis:**\n\n"
        "**🎵 Reprodução:**\n"
        "🎵 `!tocar <URL>` - Toca/adiciona música na fila\n"
        "⏸️ `!pausar` - Pausa a música atual\n"
        "▶️ `!continuar` - Continua a música pausada\n"
        "⏭️ `!proximo` - Pula para a próxima música da fila\n"
        "⏹️ `!parar` - Para tudo e desconecta\n"
        "📋 `!fila` - Mostra as músicas na fila\n"
        "🗑️ `!limpar` - Limpa toda a fila\n\n"
        "**🎼 Playlists:**\n"
        "➕ `!criar_playlist <nome>` - Cria uma playlist\n"
        "📝 `!adicionar_a_playlist <nome> <URL>` - Adiciona música à playlist\n"
        "🎵 `!tocar_playlist <nome>` - Toca uma playlist\n"
        "🗑️ `!apagar_playlist <nome>` - Apaga uma playlist\n"
        "📋 `!playlists` - Lista todas as playlists\n"
        "👁️ `!ver_playlist <nome>` - Mostra músicas da playlist\n\n"
        "**🔧 Admin:**\n"
        "🔧 `!setcookies` - [ADMIN] Atualiza cookies\n"
        "🗑️ `!clearcookies` - [ADMIN] Limpa cookies\n"
        "📤 `!export_cookies_base64` - [ADMIN] Exporta cookies"
    )
    await ctx.send(help_message)

@bot.command(name="continuar")
async def continuar(ctx):
    if not ctx.voice_client or not ctx.voice_client.is_paused():
        await ctx.send("Nada está pausado.")
        return

    ctx.voice_client.resume()
    await ctx.send("Continuando.")

@bot.command(name="pausar")
async def pausar(ctx):
    if not ctx.voice_client or not ctx.voice_client.is_playing():
        await ctx.send("Nada está tocando.")
        return

    ctx.voice_client.pause()
    await ctx.send("Pausado.")

@bot.command(name="tocar")
async def tocar(ctx, url: str):
    if not is_valid_youtube_url(url):
        await ctx.send("❌ URL do YouTube inválida.")
        return

    voice_client = await ensure_voice(ctx)
    if not voice_client:
        return
    
    queue_data = get_queue(ctx.guild.id)
    
    # Get video info for title
    await ctx.send("🔍 Obtendo informações...")
    loop = asyncio.get_running_loop()
    video_info, error = await loop.run_in_executor(None, get_video_info, url)
    title = video_info.get('title', 'Sem título') if video_info else 'Sem título'
    
    # If already playing, add to queue
    if voice_client.is_playing() or voice_client.is_paused():
        queue_data['queue'].append((url, title))
        position = len(queue_data['queue'])
        await ctx.send(f"➕ **{title}** adicionada à fila (posição #{position})")
        return
    
    # Not playing, download and play immediately
    await ctx.send(f"⬇️ Baixando: **{title}**...")
    success, mp3_path, error = await loop.run_in_executor(None, download_mp3, url)

    if not success:
        await ctx.send(f"❌ Falha ao baixar: {error}")
        return

    queue_data['current'] = mp3_path
    audio = discord.FFmpegPCMAudio(mp3_path, executable=FFMPEG_PATH)

    def after_play(err):
        if err:
            print(f"Playback error: {err}")
        # Play next song when this one finishes
        asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop)

    voice_client.play(audio, after=after_play)
    await ctx.send(f"🎵 Tocando agora: **{title}**")


@bot.command(name="parar")
async def parar(ctx):
    if not ctx.voice_client:
        await ctx.send("❌ Não conectado a um canal de voz.")
        return

    queue_data = get_queue(ctx.guild.id)
    
    # Stop playback
    ctx.voice_client.stop()
    
    # Clean up current file
    if queue_data['current']:
        cleanup_file(queue_data['current'])
        queue_data['current'] = None
    
    # Clear queue
    queue_data['queue'].clear()
    
    await ctx.voice_client.disconnect()
    await ctx.send("⏹️ Parado e desconectado. Fila limpa.")


@bot.command(name="proximo")
async def proximo(ctx):
    """Skip to next song in queue"""
    if not ctx.voice_client:
        await ctx.send("❌ Não conectado a um canal de voz.")
        return
    
    queue_data = get_queue(ctx.guild.id)
    
    if not ctx.voice_client.is_playing() and not ctx.voice_client.is_paused():
        await ctx.send("❌ Nada está tocando.")
        return
    
    if not queue_data['queue']:
        await ctx.send("⏭️ Não há próxima música na fila. Parando...")
        ctx.voice_client.stop()
        return
    
    await ctx.send(f"⏭️ Pulando... ({len(queue_data['queue'])} na fila)")
    ctx.voice_client.stop()  # This triggers after_play callback which calls play_next

@bot.command(name="fila")
async def fila(ctx):
    """Show current queue"""
    queue_data = get_queue(ctx.guild.id)
    
    if not queue_data['queue'] and not queue_data['current']:
        await ctx.send("📋 A fila está vazia.")
        return
    
    message = "📋 **Fila de Músicas:**\n\n"
    
    if ctx.voice_client and ctx.voice_client.is_playing():
        message += "🎵 **Tocando agora**\n"
    
    if queue_data['queue']:
        message += "\n**Próximas:**\n"
        for i, (url, title) in enumerate(queue_data['queue'][:10], 1):
            message += f"{i}. {title}\n"
        
        if len(queue_data['queue']) > 10:
            message += f"\n... e mais {len(queue_data['queue']) - 10} música(s)"
    else:
        message += "\n_Nenhuma música na fila_"
    
    await ctx.send(message)

@bot.command(name="limpar")
async def limpar(ctx):
    """Clear the entire queue"""
    queue_data = get_queue(ctx.guild.id)
    
    if not queue_data['queue']:
        await ctx.send("📋 A fila já está vazia.")
        return
    
    count = len(queue_data['queue'])
    queue_data['queue'].clear()
    await ctx.send(f"🗑️ Fila limpa! {count} música(s) removida(s).")


# ============= PLAYLIST COMMANDS =============

@bot.command(name="criar_playlist")
async def criar_playlist_cmd(ctx, *, playlist_name: str):
    """Create a new playlist"""
    success, message = criar_playlist(playlist_name)
    if success:
        await ctx.send(f"✅ Playlist **{playlist_name}** criada!")
    else:
        await ctx.send(f"❌ {message}")

@bot.command(name="adicionar_a_playlist")
async def adicionar_a_playlist_cmd(ctx, playlist_name: str, url: str):
    """Add a song to a playlist"""
    if not is_valid_youtube_url(url):
        await ctx.send("❌ URL do YouTube inválida.")
        return
    
    # Get video info for title
    await ctx.send("🔍 Obtendo informações...")
    loop = asyncio.get_running_loop()
    video_info, error = await loop.run_in_executor(None, get_video_info, url)
    title = video_info.get('title', 'Sem título') if video_info else 'Sem título'
    
    success, message = adicionar_a_playlist(playlist_name, url, title)
    if success:
        await ctx.send(f"✅ **{title}** adicionada à playlist **{playlist_name}**! {message}")
    else:
        await ctx.send(f"❌ {message}")

@bot.command(name="tocar_playlist")
async def tocar_playlist_cmd(ctx, *, playlist_name: str):
    """Load and play a playlist"""
    voice_client = await ensure_voice(ctx)
    if not voice_client:
        return
    
    songs, error = carregar_playlist(playlist_name)
    if error:
        await ctx.send(f"❌ {error}")
        return
    
    if not songs:
        await ctx.send(f"❌ Playlist **{playlist_name}** está vazia!")
        return
    
    queue_data = get_queue(ctx.guild.id)
    
    # If already playing, add all songs to queue
    if voice_client.is_playing() or voice_client.is_paused():
        for song in songs:
            queue_data['queue'].append((song['url'], song['title']))
        await ctx.send(f"➕ Playlist **{playlist_name}** adicionada à fila! ({len(songs)} músicas)")
        return
    
    # Not playing, add first song to play now, rest to queue
    first_song = songs[0]
    await ctx.send(f"🎵 Carregando playlist **{playlist_name}** ({len(songs)} músicas)...")
    
    # Add rest to queue
    for song in songs[1:]:
        queue_data['queue'].append((song['url'], song['title']))
    
    # Download and play first song
    loop = asyncio.get_running_loop()
    success, mp3_path, error = await loop.run_in_executor(None, download_mp3, first_song['url'])
    
    if not success:
        await ctx.send(f"❌ Falha ao baixar primeira música: {error}")
        return
    
    queue_data['current'] = mp3_path
    audio = discord.FFmpegPCMAudio(mp3_path, executable=FFMPEG_PATH)
    
    def after_play(err):
        if err:
            print(f"Playback error: {err}")
        asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop)
    
    voice_client.play(audio, after=after_play)
    await ctx.send(f"🎵 Tocando playlist **{playlist_name}**: **{first_song['title']}**\n📋 {len(songs)-1} música(s) na fila")

@bot.command(name="apagar_playlist")
async def apagar_playlist_cmd(ctx, *, playlist_name: str):
    """Delete a playlist"""
    success, message = apagar_playlist(playlist_name)
    if success:
        await ctx.send(f"🗑️ Playlist **{playlist_name}** apagada!")
    else:
        await ctx.send(f"❌ {message}")

@bot.command(name="playlists")
async def playlists_cmd(ctx):
    """List all playlists"""
    playlists, error = listar_playlists()
    if error:
        await ctx.send(f"❌ {error}")
        return
    
    if not playlists:
        await ctx.send("📋 Nenhuma playlist encontrada.\nUse `!criar_playlist <nome>` para criar uma.")
        return
    
    message = "📋 **Playlists Disponíveis:**\n\n"
    for pl in playlists:
        message += f"🎵 **{pl['name']}** - {pl['count']} música(s)\n"
    
    await ctx.send(message)

@bot.command(name="ver_playlist")
async def ver_playlist_cmd(ctx, *, playlist_name: str):
    """Show songs in a playlist"""
    songs, error = carregar_playlist(playlist_name)
    if error:
        await ctx.send(f"❌ {error}")
        return
    
    if not songs:
        await ctx.send(f"📋 Playlist **{playlist_name}** está vazia!")
        return
    
    message = f"📋 **Playlist: {playlist_name}** ({len(songs)} músicas)\n\n"
    
    for i, song in enumerate(songs[:15], 1):
        message += f"{i}. {song['title']}\n"
    
    if len(songs) > 15:
        message += f"\n... e mais {len(songs) - 15} música(s)"
    
    await ctx.send(message)

# ============= END PLAYLIST COMMANDS =============


@bot.command(name="setcookies")
@commands.is_owner()
async def setcookies(ctx, *, cookies: str = None):
    """
    Atualiza o arquivo de cookies do YouTube.
    Uso 1 (texto): !setcookies <conteúdo dos cookies>
    Uso 2 (arquivo): !setcookies (anexar arquivo cookies.txt)
    Apenas o dono do bot pode usar este comando.
    """
    # Check for file attachment
    if ctx.message.attachments:
        try:
            attachment = ctx.message.attachments[0]
            if not attachment.filename.endswith('.txt'):
                await ctx.send("❌ Por favor, envie um arquivo .txt")
                return
            
            # Download file content
            cookies_content = await attachment.read()
            cookies_content = cookies_content.decode('utf-8')
            
            # Write to cookies file with Unix line endings (newline='\n' for Linux compatibility)
            with open(YT_COOKIES_FILE, 'w', encoding='utf-8', newline='\n') as f:
                f.write(cookies_content)
            
            # Validate cookies file
            if validate_cookies_file(YT_COOKIES_FILE):
                await ctx.send("✅ Cookies atualizados com sucesso via arquivo!")
            else:
                await ctx.send("⚠️ Arquivo recebido, mas header pode estar inválido. Verifique o formato.")
            
            # Delete the command message for security
            try:
                await ctx.message.delete()
            except:
                pass
        except Exception as e:
            await ctx.send(f"❌ Erro ao processar arquivo: {str(e)}")
    
    # Check for text content
    elif cookies:
        try:
            # Write cookies to file with Unix line endings (newline='\n' for Linux compatibility)
            with open(YT_COOKIES_FILE, 'w', encoding='utf-8', newline='\n') as f:
                f.write(cookies)
            
            # Validate cookies file
            if validate_cookies_file(YT_COOKIES_FILE):
                await ctx.send("✅ Cookies atualizados com sucesso!")
            else:
                await ctx.send("⚠️ Cookies recebidos, mas header pode estar inválido. Esperado: '# Netscape HTTP Cookie File'")
            
            # Delete the command message for security
            try:
                await ctx.message.delete()
            except:
                pass
                
        except Exception as e:
            await ctx.send(f"❌ Erro ao atualizar cookies: {str(e)}")
    
    else:
        await ctx.send("❌ Use um dos formatos:\n`!setcookies <cookies>` ou anexe um arquivo .txt")


@bot.command(name="export_cookies_base64")
@commands.is_owner()
async def export_cookies_base64(ctx):
    """
    Exporta os cookies atuais em formato base64 para usar como env var no Render.
    Uso: !export_cookies_base64
    Apenas o dono do bot pode usar este comando.
    """
    try:
        if not os.path.exists(YT_COOKIES_FILE):
            await ctx.send("❌ Arquivo de cookies não encontrado")
            return
        
        with open(YT_COOKIES_FILE, 'r', encoding='utf-8') as f:
            cookies_content = f.read()
        
        cookies_base64 = base64.b64encode(cookies_content.encode('utf-8')).decode('utf-8')
        
        # Split into chunks for Discord (max 2000 chars per message)
        chunk_size = 1900
        chunks = [cookies_base64[i:i+chunk_size] for i in range(0, len(cookies_base64), chunk_size)]
        
        await ctx.send(f"**Base64 Cookies para Render (Em {len(chunks)} parte(s)):**\n\nCopie o valor completo de TODOS os pedaços abaixo:")
        
        for i, chunk in enumerate(chunks, 1):
            await ctx.send(f"```\nParte {i}/{len(chunks)}:\n{chunk}\n```")
        
        await ctx.send(f"⚠️ **IMPORTANTE:** Cole o valor **COMPLETO** (unindo todas as {len(chunks)} parte(s)) na env var `YT_COOKIES_BASE64` do Render, sem quebras de linha!")
            
    except Exception as e:
        await ctx.send(f"❌ Erro ao exportar cookies: {str(e)}")
@commands.is_owner()
async def clearcookies(ctx):
    """
    Limpa/reseta o arquivo de cookies do YouTube.
    Uso: !clearcookies
    Apenas o dono do bot pode usar este comando.
    """
    try:
        # Clear cookies file with Unix line endings (newline='\n' for Linux compatibility)
        with open(YT_COOKIES_FILE, 'w', encoding='utf-8', newline='\n') as f:
            f.write("# Netscape HTTP Cookie File\n# This file is generated by yt-dlp. Do not edit.\n\n")
        
        await ctx.send("✅ Cookies foram resetados! O bot agora vai usar a configuração padrão (sem cookies).")
            
    except Exception as e:
        await ctx.send(f"❌ Erro ao limpar cookies: {str(e)}")
    
def run_flask():
    app.run(host="0.0.0.0", port=5000, debug=False)


if __name__ == '__main__':
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not set.")

    bot.run(DISCORD_TOKEN)