from flask import Flask, request, jsonify
import yt_dlp
import re

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
_local_ffmpeg = os.path.join(BASE_DIR, "ffmpeg", "bin", "ffmpeg.exe")
FFMPEG_PATH = os.getenv("FFMPEG_PATH") or (_local_ffmpeg if os.path.exists(_local_ffmpeg) else "ffmpeg")
MAX_MP3_BYTES = 100 * 1024 * 1024
MP3_BITRATE = "192k"
MP3_SAMPLE_RATE = "44100"
MP3_CHANNELS = "2"

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
current_tracks = {}

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
            {"name": "Com cookies", "cookies": True, "format": "bestaudio/best"},
            {"name": "Com cookies (fallback)", "cookies": True, "format": "480"},
            {"name": "Com PO Token (se disponível)", "cookies": True, "format": "bestaudio/best"} if YT_PO_TOKEN else None,
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
                    'quiet': False,
                    'no_warnings': False,
                    'socket_timeout': 30,
                    'retries': 3,
                    'fragment_retries': 3,
                    'http_headers': {
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                    },
                    'postprocessors': [{
                        'key': 'FFmpegExtractAudio',
                        'preferredcodec': 'mp3',
                        'preferredquality': '192',
                    }],
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


def cleanup_track(guild_id):
    path = current_tracks.pop(guild_id, None)
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


def cleanup_downloads_dir():
    if not os.path.isdir(DOWNLOAD_DIR):
        return
    for name in os.listdir(DOWNLOAD_DIR):
        path = os.path.join(DOWNLOAD_DIR, name)
        if os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                pass


@bot.event
async def on_ready():
    print(f"Bot conectado como {bot.user}")


@bot.command(name="ajuda")
async def ajuda(ctx):
    help_message = (
        "Comandos disponíveis:\n"
        "!tocar <URL do YouTube> - Toca o áudio do vídeo no canal de voz.\n"
        "!parar - Para a reprodução e desconecta do canal de voz.\n"
        "!pausar - Pausa a reprodução atual.\n"
        "!continuar - Continua a reprodução pausada.\n"
        "!pular - Para a reprodução atual, mas permanece conectado ao canal de voz.\n"
        "!setcookies <cookies> - [ADMIN] Atualiza os cookies do YouTube.\n"
        "!clearcookies - [ADMIN] Limpa/reseta os cookies do YouTube.\n"
        "!export_cookies_base64 - [ADMIN] Exporta cookies em base64 para Render."
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
        await ctx.send("URL do YouTube inválida.")
        return

    voice_client = await ensure_voice(ctx)
    if not voice_client:
        return

    await ctx.send("Baixando a droga do áudio...")
    cleanup_downloads_dir()
    loop = asyncio.get_running_loop()
    success, mp3_path, error = await loop.run_in_executor(None, download_mp3, url)

    if not success:
        await ctx.send(f"Falha ao baixar: {error}")
        return

    if voice_client.is_playing():
        voice_client.stop()

    cleanup_track(ctx.guild.id)
    current_tracks[ctx.guild.id] = mp3_path

    audio = discord.FFmpegPCMAudio(mp3_path, executable=FFMPEG_PATH)

    def after_play(err):
        cleanup_track(ctx.guild.id)
        if err:
            print(f"Playback error: {err}")
        asyncio.run_coroutine_threadsafe(voice_client.disconnect(), bot.loop)

    voice_client.play(audio, after=after_play)
    await ctx.send("Tocando agora.")


@bot.command(name="parar")
async def parar(ctx):
    if not ctx.voice_client:
        await ctx.send("Não conectado a um canal de voz.")
        return

    ctx.voice_client.stop()
    cleanup_track(ctx.guild.id)
    await ctx.voice_client.disconnect()
    await ctx.send("Parado e limpo.")


@bot.command(name="pular")
async def pular(ctx):
    if not ctx.voice_client or not ctx.voice_client.is_playing():
        await ctx.send("Nada está tocando.")
        return

    ctx.voice_client.stop()
    cleanup_track(ctx.guild.id)
    await ctx.send("Pulou e limpo.")


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