import asyncio
import io
import os
import random
import sqlite3
import urllib.parse
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
import yt_dlp

# ==============================================================================
# 🗄️ BANCO DE DADOS & CONFIGURAÇÃO INICIAL (CAMINHO ABSOLUTO)
# ==============================================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "database.db")


def init_db():
  """Inicializa a tabela de configurações por servidor no SQLite."""
  conn = sqlite3.connect(DB_PATH)
  cursor = conn.cursor()
  cursor.execute("""
        CREATE TABLE IF NOT EXISTS guild_settings (
            guild_id TEXT PRIMARY KEY,
            welcome_channel_id TEXT
        )
    """)
  conn.commit()
  conn.close()


init_db()

# Inicializa dependências de áudio (FFmpeg)
try:
  import static_ffmpeg

  static_ffmpeg.add_paths()
except Exception as e:
  print(f"⚠️ Aviso sobre FFmpeg: {e}")

# Configuração de Intents do Discord
intents = discord.Intents.default()
intents.voice_states = True
intents.guilds = True
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ==============================================================================
# 🔐 TRAVA ESPECIAL: PERMISSÃO ADMINISTRATIVA COMPLETA
# ==============================================================================


def tem_todas_as_permissoes(member: discord.Member) -> bool:
  """Verifica se o membro possui todas as permissões (Administrador) no servidor."""
  return member.guild_permissions.administrator


# ==============================================================================
# 🎵 ESTRUTURAS & CONFIGURAÇÕES DE MÚSICA
# ==============================================================================

song_queue = {}
played_history = {}
current_song = {}
join_timers = {}

NOTICE_CHANNEL_ID = 1551718525733965885

YTDL_OPTIONS = {
    'format': 'ba/b',
    'extractaudio': True,
    'audioformat': 'mp3',
    'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'ytsearch',
    'source_address': '0.0.0.0',
}

FFMPEG_OPTIONS = {
    'before_options': (
        '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5'
    ),
    'options': '-vn -b:a 96k',
}

ytdl = yt_dlp.YoutubeDL(YTDL_OPTIONS)

# ==============================================================================
# 🛠️ FUNÇÕES AUXILIARES & UTILITÁRIOS
# ==============================================================================


async def get_anime_gif(action: str):
  """Busca GIFs dinâmicos na API Nekos.best para interações sociais."""
  url = f'https://nekos.best/api/v2/{action}'
  try:
    async with aiohttp.ClientSession() as session:
      async with session.get(url, timeout=10) as response:
        if response.status == 200:
          data = await response.json()
          if 'results' in data and len(data['results']) > 0:
            return data['results'][0]['url']
  except Exception as e:
    print(f'⚠️ Erro ao buscar GIF ({action}): {e}')
  return None


def cancel_join_timer(guild_id: int):
  if guild_id in join_timers and join_timers[guild_id]:
    join_timers[guild_id].cancel()
    join_timers[guild_id] = None


async def join_timeout_task(guild: discord.Guild):
  try:
    await asyncio.sleep(300)
    vc = guild.voice_client
    if vc and vc.is_connected() and not vc.is_playing() and not vc.is_paused():
      await vc.disconnect()
      channel = guild.get_channel(NOTICE_CHANNEL_ID)
      if channel:
        await channel.send(
            '⏳ **Tempo esgotado:** Fiquei 5 minutos sem tocar nenhuma música'
            ' e saí da call para economizar recursos!'
        )
  except asyncio.CancelledError:
    pass
  finally:
    join_timers[guild.id] = None


def format_duration(seconds):
  if not seconds:
    return 'Ao Vivo / Desconhecido'
  seconds = int(seconds)
  minutes, secs = divmod(seconds, 60)
  hours, minutes = divmod(minutes, 60)
  if hours > 0:
    return f'{hours:02d}:{minutes:02d}:{secs:02d}'
  return f'{minutes:02d}:{secs:02d}'


def create_song_embed(song_info: dict) -> discord.Embed:
  embed = discord.Embed(
      title=f"🎵 {song_info['title']}",
      url=song_info.get('webpage_url', ''),
      color=discord.Color.blurple(),
  )
  if song_info.get('thumbnail'):
    embed.set_thumbnail(url=song_info['thumbnail'])
  embed.add_field(name='🌐 Plataforma', value=song_info['source'], inline=True)
  embed.add_field(name='⏱️ Duração', value=song_info['duration'], inline=True)
  embed.add_field(
      name='👤 Canal / Artista', value=song_info['uploader'], inline=False
  )
  embed.set_footer(text='Discord Bot • Player de Áudio HD')
  return embed


def play_next(interaction: discord.Interaction):
  guild_id = interaction.guild_id

  if guild_id in current_song and current_song[guild_id]:
    if guild_id not in played_history:
      played_history[guild_id] = []
    played_history[guild_id].append(current_song[guild_id])
    current_song[guild_id] = None

  if guild_id in song_queue and len(song_queue[guild_id]) > 0:
    next_song = song_queue[guild_id].pop(0)
    current_song[guild_id] = next_song
    vc = interaction.guild.voice_client
    if vc and vc.is_connected():
      source = discord.FFmpegPCMAudio(next_song['url'], **FFMPEG_OPTIONS)
      vc.play(source, after=lambda e: play_next(interaction))

      embed = create_song_embed(next_song)
      asyncio.run_coroutine_threadsafe(
          interaction.channel.send(
              content='▶️ **Tocando agora a próxima da fila:**', embed=embed
          ),
          bot.loop,
      )


async def extract_song_data(query: str, platform_label: str = '🔴 YouTube'):
  loop = asyncio.get_event_loop()
  data = await loop.run_in_executor(
      None, lambda: ytdl.extract_info(query, download=False)
  )
  if 'entries' in data and len(data['entries']) > 0:
    data = data['entries'][0]

  return {
      'title': data.get('title', 'Música Sem Título'),
      'url': data['url'],
      'webpage_url': data.get('webpage_url', query),
      'thumbnail': data.get('thumbnail', None),
      'duration': format_duration(data.get('duration')),
      'uploader': (
          data.get('uploader')
          or data.get('channel')
          or data.get('uploader_id')
          or 'Desconhecido'
      ),
      'source': platform_label,
  }


async def process_and_play_song(
    interaction: discord.Interaction, query: str, platform_label: str
):
  member = interaction.guild.get_member(
      interaction.user.id
  ) or await interaction.guild.fetch_member(interaction.user.id)
  if not member or not member.voice or not member.voice.channel:
    await interaction.followup.send(
        '❌ Você precisa estar em um canal de voz!', ephemeral=True
    )
    return

  cancel_join_timer(interaction.guild_id)

  channel = member.voice.channel
  vc = interaction.guild.voice_client
  if not vc or not vc.is_connected():
    vc = await channel.connect()
  elif vc.channel.id != channel.id:
    await vc.move_to(channel)

  try:
    song_info = await extract_song_data(query, platform_label)
  except Exception:
    await interaction.followup.send(
        '❌ Erro ao buscar ou processar a mídia fornecida.'
    )
    return

  guild_id = interaction.guild_id
  if guild_id not in song_queue:
    song_queue[guild_id] = []

  embed = create_song_embed(song_info)

  if vc.is_playing() or vc.is_paused():
    song_queue[guild_id].append(song_info)
    await interaction.followup.send(
        content=(
            '➕ **Música adicionada à fila (Posição'
            f' #{len(song_queue[guild_id])}):**'
        ),
        embed=embed,
    )
  else:
    try:
      current_song[guild_id] = song_info
      source = discord.FFmpegPCMAudio(song_info['url'], **FFMPEG_OPTIONS)
      vc.play(source, after=lambda e: play_next(interaction))
      await interaction.followup.send(
          content='▶️ **Tocando agora:**', embed=embed
      )
    except Exception:
      await interaction.followup.send(
          '❌ Ocorreu uma falha no reproductor de áudio.'
      )


# ==============================================================================
# 🎛️ MODAIS E SELETORES INTERATIVOS
# ==============================================================================


class PlayGenericURLModal(discord.ui.Modal):

  def __init__(self, platform_name: str, emoji_prefix: str):
    super().__init__(title=f'Tocar via {platform_name}')
    self.platform_name = platform_name
    self.emoji_prefix = emoji_prefix
    self.url_input = discord.ui.TextInput(
        label=f'Link / URL do {platform_name}',
        placeholder='Cole o endereço completo aqui...',
        required=True,
    )
    self.add_item(self.url_input)

  async def on_submit(self, interaction: discord.Interaction):
    await interaction.response.defer()
    url = self.url_input.value
    source_label = f'{self.emoji_prefix} {self.platform_name}'
    await process_and_play_song(interaction, url, source_label)


class PlayNameModal(discord.ui.Modal, title='Pesquisar Música por Nome'):

  nome_input = discord.ui.TextInput(
      label='Nome da Música / Artista',
      placeholder='Ex: Imagine Dragons - Bones',
      required=True,
  )

  async def on_submit(self, interaction: discord.Interaction):
    await interaction.response.defer()
    nome = self.nome_input.value.strip()
    search_query = f'ytsearch:{nome}'
    source_label = '🔴 YouTube'
    await process_and_play_song(interaction, search_query, source_label)


class PlaySelectionView(discord.ui.View):

  def __init__(self):
    super().__init__(timeout=60)

  @discord.ui.select(
      placeholder='Selecione a fonte do áudio...',
      options=[
          discord.SelectOption(
              label='YouTube (Link Directo)', value='youtube_url', emoji='🔴'
          ),
          discord.SelectOption(
              label='SoundCloud (Link)', value='soundcloud_url', emoji='🟠'
          ),
          discord.SelectOption(
              label='Twitch (Link)', value='twitch_url', emoji='🟣'
          ),
          discord.SelectOption(
              label='Bandcamp (Link)', value='bandcamp_url', emoji='🔵'
          ),
          discord.SelectOption(
              label='Vimeo (Link)', value='vimeo_url', emoji='🔷'
          ),
          discord.SelectOption(
              label='Pesquisar por Nome', value='nome', emoji='🔎'
          ),
      ],
  )
  async def select_callback(
      self, interaction: discord.Interaction, select: discord.ui.Select
  ):
    val = select.values[0]
    if val == 'youtube_url':
      await interaction.response.send_modal(
          PlayGenericURLModal('YouTube', '🔴')
      )
    elif val == 'soundcloud_url':
      await interaction.response.send_modal(
          PlayGenericURLModal('SoundCloud', '🟠')
      )
    elif val == 'twitch_url':
      await interaction.response.send_modal(PlayGenericURLModal('Twitch', '🟣'))
    elif val == 'bandcamp_url':
      await interaction.response.send_modal(
          PlayGenericURLModal('Bandcamp', '🔵')
      )
    elif val == 'vimeo_url':
      await interaction.response.send_modal(PlayGenericURLModal('Vimeo', '🔷'))
    elif val == 'nome':
      await interaction.response.send_modal(PlayNameModal())


# ==============================================================================
# ⚡ EVENTOS DO SISTEMA
# ==============================================================================


@bot.event
async def on_ready():
  print('\n' + '=' * 50)
  print(f'🤖 Bot online com sucesso | Usuário: {bot.user}')
  try:
    synced = await bot.tree.sync()
    print(f'✅ {len(synced)} Comandos Slash (/) sincronizados globalmente!')
  except Exception as e:
    print(f'❌ Falha ao sincronizar comandos: {e}')

  await bot.change_presence(
      activity=discord.Streaming(
          name='📡 /ajuda • Bot em BETA', url='https://www.twitch.tv/discord'
      ),
      status=discord.Status.online,
  )
  print('=' * 50 + '\n')


@bot.event
async def on_member_join(member: discord.Member):
  conn = sqlite3.connect(DB_PATH)
  cursor = conn.cursor()
  cursor.execute(
      'SELECT welcome_channel_id FROM guild_settings WHERE guild_id = ?',
      (str(member.guild.id),),
  )
  row = cursor.fetchone()
  conn.close()

  if row and row[0]:
    try:
      channel = member.guild.get_channel(int(row[0]))
      if channel:
        embed = discord.Embed(
            title=f'🎉 Seja muito bem-vindo(a) ao {member.guild.name}!',
            description=(
                f'Olá {member.mention}, é um prazer enorme ter você conosco!\n\n'
                '📌 **Dicas para começar:**\n'
                '• Confira as regras no canal de avisos.\n'
                '• Sinta-se livre para interagir no chat.\n'
                '• Digite `/ajuda` para conhecer todos os meus comandos!'
            ),
            color=discord.Color.green(),
        )
        if member.display_avatar:
          embed.set_thumbnail(url=member.display_avatar.url)

        embed.set_image(
            url='https://media.giphy.com/media/v1.Y2lkPTc5MGI3NjExbnE3MndycXNyeGNwbzAxeDV6MG4xNWlndGRqOHJjcjA0eXJ0dnprciZlcD12MV9pbnRlcm5hbF9naWZfYnlfaWQmY3Q9Zw/3o7TKSjRrfIPjeiVyM/giphy.gif'
        )
        embed.add_field(
            name='👥 Contador de Membros',
            value=f'Você é o membro **#{member.guild.member_count}**!',
            inline=True,
        )
        embed.set_footer(text=f'ID do Usuário: {member.id}')

        await channel.send(content=f'👋 {member.mention}', embed=embed)
    except Exception as e:
      print(f'⚠️ Erro ao enviar boas-vindas: {e}')


# ==============================================================================
# 🎨 COMANDO DE AJUDA DETALHADO
# ==============================================================================


@bot.tree.command(
    name='ajuda',
    description='Exibe o painel de ajuda detalhado com todos os comandos.',
)
async def ajuda(interaction: discord.Interaction):
  embed = discord.Embed(
      title='✨ CENTRAL DE CONTROLE E AJUDA DO BOT',
      description=(
          'Bem-vindo ao painel explicativo completo! Veja abaixo a lista de'
          ' todos os comandos categorizados com suas devidas funções:\n\n'
          '▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬'
      ),
      color=discord.Color.blurple(),
  )

  if bot.user and bot.user.display_avatar:
    embed.set_thumbnail(url=bot.user.display_avatar.url)

  embed.set_image(
      url='https://media.giphy.com/media/v1.Y2lkPTc5MGI3NjExM3Y5YnkxeTZycXQyd3BhZDJyMWk1cTN3eG9yMnh6OXFlNWFid3ZxeCZlcD12MV9pbnRlcm5hbF9naWZfYnlfaWQmY3Q9Zw/26tn33aiTi1jkl6H6/giphy.gif'
  )

  embed.add_field(
      name='👑 **SISTEMA & CONFIGURAÇÃO (RESTRITO: ADMINISTRADOR)**',
      value=(
          '`/setboasvindas [canal]`\n'
          '└ ⚙️ *Define o canal fixo para mensagens de boas-vindas.*\n'
          '`/testboasvindas [membro] [canal]`\n'
          '└ 🧪 *Simula o card de boas-vindas com GIF e contador de'
          ' membros.*\n'
          '`/clean [quantidade]`\n'
          '└ 🧹 *Apaga mensagens em massa no canal (incluindo fixadas).*\n'
          '▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬'
      ),
      inline=False,
  )

  embed.add_field(
      name='🎵 **MÚSICA & PLAYER DE ÁUDIO**',
      value=(
          '`/join`\n'
          '└ 🔊 *Conecta o bot na call com temporizador de 5min por'
          ' inatividade.*\n'
          '`/play`\n'
          '└ 🎶 *Abre o menu interativo (YouTube, SoundCloud, Twitch,'
          ' etc).*\n'
          '`/skip`\n'
          '└ ⏭️ *Pula a música atual para a próxima da fila (Exclusivo'
          ' Admin).*\n'
          '`/recomecar`\n'
          '└ 🔄 *Reinicia a música atual do início (Exclusivo Admin).*\n'
          '`/reiniciarfila`\n'
          '└ 🔁 *Reinicia a playlist completa do primeiro item (Exclusivo'
          ' Admin).*\n'
          '`/stop`\n'
          '└ 🛑 *Interrompe o player, limpa a fila e desconecta (Exclusivo'
          ' Admin).*\n'
          '▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬'
      ),
      inline=False,
  )

  embed.add_field(
      name='🔊 **CONTROLE DE VOZ**',
      value=(
          '`/muteall`\n'
          '└ 🔇 *Muta todos os membros presentes na sua call.*\n'
          '`/unmuteall`\n'
          '└ 🔊 *Desmuta todos os membros da sua call.*\n'
          '▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬'
      ),
      inline=False,
  )

  embed.add_field(
      name='💞 **AÇÕES SOCIAIS (INTERAÇÕES COM GIF)**',
      value=(
          '`/abracar [membro]` └ 🤗 *Manda um abraço carinhoso com GIF de'
          ' anime.*\n'
          '`/beijar [membro]` └ 💋 *Manda um beijo apaixonado no chat.*\n'
          '`/tapa [membro]` └ 🖐️ *Dá um tapa de brincadeira em um amigo.*\n'
          '`/cafune [membro]` └ 🫳 *Faz carinho na cabeça de outro membro.*\n'
          '`/cutucar [membro]` └ 👉 *Cutuca alguém para chamar atenção.*\n'
          '`/tocaaqui [membro]` └ 🖐️ *Manda um Toca Aqui (High Five).*\n'
          '`/morder [membro]` └ 🦷 *Dá uma mordida divertida em alguém.*\n'
          '`/cosquinhas [membro]` └ 🤏 *Faz cosquinhas até a pessoa rir.*\n'
          '`/ship [membro1] [membro2]` └ 💕 *Calcula a % de amor e gera nome do'
          ' casal.*\n'
          '▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬'
      ),
      inline=False,
  )

  embed.add_field(
      name='🎮 **JOGOS & ENTRETENIMENTO**',
      value=(
          '`/jokempo [opcao]` └ 🪨📄✂️ *Jogue Pedra, Papel ou Tesoura com o'
          ' bot.*\n'
          '`/adivinhar [numero]` └ 🎯 *Tente acertar o número secreto de 1 a'
          ' 10.*\n'
          '`/8ball [pergunta]` └ 🔮 *Receba respostas da Bola Mágica do'
          ' destino.*\n'
          '`/dado [lados]` └ 🎲 *Rola um dado com a quantidade de lados'
          ' definida.*\n'
          '`/moeda` └ 🪙 *Joga uma moeda e cai Cara ou Coroa.*\n'
          '`/escolher [opções]` └ 🤔 *O bot escolhe entre opções separadas por'
          ' vírgula.*\n'
          '▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬'
      ),
      inline=False,
  )

  embed.add_field(
      name='🎨 **UTILITÁRIOS & FERRAMENTAS**',
      value=(
          '`/image [prompt]` └ 🎨 *Gera artes em alta definição usando IA.*\n'
          '`/meme [descricao] (imagem)` └ 🎭 *Busca memes na web ou cria o seu'
          ' próprio.*\n'
          '`/conquista [texto]` └ 🏆 *Gera uma imagem de conquista do'
          ' Minecraft.*\n'
          '`/avatar [membro]` └ 🖼️ *Exibe e disponibiliza download da foto em'
          ' HD.*\n'
          '`/calcule [expressao]` └ 🧮 *Resolve expressões matemáticas'
          ' avançadas.*\n'
          '▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬'
      ),
      inline=False,
  )

  embed.set_footer(
      text='🛠️ BOT EM FASE BETA • Podem ocorrer atualizações • Comandos de'
      ' gestão exigem permissão de Administrador.'
  )
  await interaction.response.send_message(embed=embed, ephemeral=True)


# ==============================================================================
# ⚙️ COMANDOS DE CONFIGURAÇÃO (RESTRITOS A ADMINISTRADORES)
# ==============================================================================


@bot.tree.command(
    name='setboasvindas',
    description='Define o canal de boas-vindas (Exclusivo: Requer Administrador).',
)
@app_commands.describe(canal='Selecione o canal de texto desejado')
async def setboasvindas(
    interaction: discord.Interaction, canal: discord.TextChannel
):
  await interaction.response.defer(ephemeral=True)

  if not tem_todas_as_permissoes(interaction.user):
    await interaction.followup.send(
        '⛔ **Acesso Negado!** Você precisa ter um cargo com permissão de'
        ' **Administrador** para executar este comando.',
        ephemeral=True,
    )
    return

  conn = sqlite3.connect(DB_PATH)
  cursor = conn.cursor()
  cursor.execute(
      """
        INSERT INTO guild_settings (guild_id, welcome_channel_id)
        VALUES (?, ?)
        ON CONFLICT(guild_id) DO UPDATE SET welcome_channel_id=excluded.welcome_channel_id
    """,
      (str(interaction.guild_id), str(canal.id)),
  )
  conn.commit()
  conn.close()

  await interaction.followup.send(
      f'✅ Canal de boas-vindas alterado com sucesso para {canal.mention}!',
      ephemeral=True,
  )


@bot.tree.command(
    name='testboasvindas',
    description=(
        'Simula a mensagem de boas-vindas (Exclusivo: Requer Administrador).'
    ),
)
@app_commands.describe(
    membro='Membro para simular (Padrão: Você)',
    canal='Canal de destino (Opcional)',
)
async def testboasvindas(
    interaction: discord.Interaction,
    membro: discord.Member = None,
    canal: discord.TextChannel = None,
):
  await interaction.response.defer(ephemeral=True)

  if not tem_todas_as_permissoes(interaction.user):
    await interaction.followup.send(
        '⛔ **Acesso Negado!** Você precisa ter um cargo com permissão de'
        ' **Administrador** para executar este comando.',
        ephemeral=True,
    )
    return

  target_member = membro or interaction.user
  target_channel = canal

  if not target_channel:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        'SELECT welcome_channel_id FROM guild_settings WHERE guild_id = ?',
        (str(interaction.guild_id),),
    )
    row = cursor.fetchone()
    conn.close()

    if row and row[0]:
      target_channel = interaction.guild.get_channel(int(row[0]))

  if not target_channel:
    await interaction.followup.send(
        '❌ Nenhum canal informado ou configurado! Use a opção `canal:` ou'
        ' defina primeiro com `/setboasvindas`.',
        ephemeral=True,
    )
    return

  try:
    embed = discord.Embed(
        title=f'🎉 Seja muito bem-vindo(a) ao {interaction.guild.name}!',
        description=(
            f'Olá {target_member.mention}, é um prazer ter você conosco!\n\n📌'
            ' Sinta-se à vontade para interagir no chat e conferir nossas'
            ' regras.\n💬 Use o comando `/ajuda` para ver o que posso fazer!'
        ),
        color=discord.Color.green(),
    )
    if target_member.display_avatar:
      embed.set_thumbnail(url=target_member.display_avatar.url)

    embed.set_image(
        url='https://media.giphy.com/media/v1.Y2lkPTc5MGI3NjExbnE3MndycXNyeGNwbzAxeDV6MG4xNWlndGRqOHJjcjA0eXJ0dnprciZlcD12MV9pbnRlcm5hbF9naWZfYnlfaWQmY3Q9Zw/3o7TKSjRrfIPjeiVyM/giphy.gif'
    )

    embed.add_field(
        name='👥 Contador de Membros',
        value=f'Membro **#{interaction.guild.member_count}**',
        inline=True,
    )
    embed.set_footer(
        text=f'ID do Usuário: {target_member.id} • (Mensagem de Teste)'
    )

    await target_channel.send(content=f'👋 {target_member.mention}', embed=embed)

    await interaction.followup.send(
        f'✅ Mensagem de teste enviada em {target_channel.mention}!',
        ephemeral=True,
    )
  except Exception as e:
    await interaction.followup.send(
        f'❌ Erro ao enviar teste: {e}', ephemeral=True
    )


@bot.tree.command(
    name='clean',
    description=(
        'Apaga mensagens do chat (Exclusivo: Requer Administrador).'
    ),
)
@app_commands.describe(quantidade='Quantidade de mensagens para apagar')
async def clean(interaction: discord.Interaction, quantidade: int = 100):
  await interaction.response.defer(ephemeral=True)

  if not tem_todas_as_permissoes(interaction.user):
    await interaction.followup.send(
        '⛔ **Acesso Negado!** Você precisa ter um cargo com permissão de'
        ' **Administrador** para executar este comando.',
        ephemeral=True,
    )
    return

  channel = interaction.channel
  deleted_count = 0

  try:
    async for message in channel.history(limit=quantidade):
      if message.pinned:
        try:
          await message.unpin()
        except Exception:
          pass

      try:
        await message.delete()
        deleted_count += 1
        await asyncio.sleep(0.3)
      except discord.HTTPException:
        pass

    await interaction.followup.send(
        '🧹 **Limpeza Concluída!** Removidas'
        f' `{deleted_count}` mensagens.',
        ephemeral=True,
    )
  except Exception as e:
    await interaction.followup.send(
        f'❌ Erro ao executar limpeza: {e}', ephemeral=True
    )


# ==============================================================================
# 🎵 COMANDOS DE MÚSICA & REPRODUÇÃO
# ==============================================================================


@bot.tree.command(
    name='join', description='Conecta o bot ao seu canal de voz.'
)
async def join(interaction: discord.Interaction):
  await interaction.response.defer()
  member = interaction.guild.get_member(
      interaction.user.id
  ) or await interaction.guild.fetch_member(interaction.user.id)

  if not member or not member.voice or not member.voice.channel:
    await interaction.followup.send(
        '❌ Você precisa estar em um canal de voz!', ephemeral=True
    )
    return

  channel = member.voice.channel
  vc = interaction.guild.voice_client

  if vc is not None:
    await vc.move_to(channel)
  else:
    await channel.connect()

  cancel_join_timer(interaction.guild_id)
  join_timers[interaction.guild_id] = asyncio.create_task(
      join_timeout_task(interaction.guild)
  )

  await interaction.followup.send(
      f'🔊 Conectado ao canal de voz **{channel.name}**!'
  )


@bot.tree.command(
    name='play', description='Abre o menu interativo para tocar músicas.'
)
async def play(interaction: discord.Interaction):
  member = interaction.guild.get_member(
      interaction.user.id
  ) or await interaction.guild.fetch_member(interaction.user.id)
  if not member or not member.voice or not member.voice.channel:
    await interaction.response.send_message(
        '❌ Você precisa estar em um canal de voz primeiro!', ephemeral=True
    )
    return

  view = PlaySelectionView()
  await interaction.response.send_message(
      '🎵 **Escolha como deseja adicionar a música:**', view=view, ephemeral=True
  )


@bot.tree.command(
    name='recomecar', description='Reinicia a execução da música atual.'
)
@app_commands.checks.has_permissions(administrator=True)
async def recomecar(interaction: discord.Interaction):
  await interaction.response.defer()
  guild_id = interaction.guild_id
  vc = interaction.guild.voice_client

  if not vc or not vc.is_connected() or not current_song.get(guild_id):
    await interaction.followup.send(
        '❌ Nenhuma música sendo reproduzida no momento.', ephemeral=True
    )
    return

  song_info = current_song[guild_id]
  vc.stop()

  source = discord.FFmpegPCMAudio(song_info['url'], **FFMPEG_OPTIONS)
  vc.play(source, after=lambda e: play_next(interaction))

  embed = create_song_embed(song_info)
  await interaction.followup.send(
      content='🔄 **Música recomeçada do início:**', embed=embed
  )


@bot.tree.command(
    name='reiniciarfila',
    description='Reinicia toda a playlist/fila desde a primeira música.',
)
@app_commands.checks.has_permissions(administrator=True)
async def reiniciarfila(interaction: discord.Interaction):
  await interaction.response.defer()
  guild_id = interaction.guild_id
  vc = interaction.guild.voice_client

  if not vc or not vc.is_connected():
    await interaction.followup.send(
        '❌ Não estou em um canal de voz.', ephemeral=True
    )
    return

  history = played_history.get(guild_id, [])
  active = [current_song[guild_id]] if current_song.get(guild_id) else []
  queue = song_queue.get(guild_id, [])

  full_playlist = history + active + queue

  if not full_playlist:
    await interaction.followup.send(
        '❌ Nenhuma histórico ou fila ativa encontrada.', ephemeral=True
    )
    return

  played_history[guild_id] = []
  song_queue[guild_id] = full_playlist.copy()
  current_song[guild_id] = None

  if vc.is_playing() or vc.is_paused():
    vc.stop()

  play_next(interaction)
  await interaction.followup.send(
      '🔄 **Playlist reiniciada a partir da primeira faixa!**'
  )


@bot.tree.command(
    name='skip', description='Pula a música atual para a próxima.'
)
@app_commands.checks.has_permissions(administrator=True)
async def skip(interaction: discord.Interaction):
  await interaction.response.defer()
  vc = interaction.guild.voice_client

  if not vc or not vc.is_connected():
    await interaction.followup.send(
        '❌ Não estou conectado em nenhum canal de voz.', ephemeral=True
    )
    return

  if not vc.is_playing():
    await interaction.followup.send(
        '❌ Nenhuma faixa tocando no momento.', ephemeral=True
    )
    return

  vc.stop()
  await interaction.followup.send('⏭️ **Música pulada com sucesso!**')


@bot.tree.command(
    name='stop',
    description='Interrompe o player, limpa a fila e desconecta o bot.',
)
@app_commands.checks.has_permissions(administrator=True)
async def stop(interaction: discord.Interaction):
  await interaction.response.defer()
  vc = interaction.guild.voice_client
  guild_id = interaction.guild_id

  cancel_join_timer(guild_id)

  if not vc or not vc.is_connected():
    await interaction.followup.send(
        '❌ Não estou em um canal de voz.', ephemeral=True
    )
    return

  if guild_id in song_queue:
    song_queue[guild_id].clear()
  if guild_id in played_history:
    played_history[guild_id].clear()
  current_song[guild_id] = None

  if vc.is_playing() or vc.is_paused():
    vc.stop()
  await vc.disconnect()

  await interaction.followup.send(
      '🛑 **Player de áudio desligado e bot desconectado.**'
  )


# ==============================================================================
# 🔊 MODERAÇÃO DE ÁUDIO
# ==============================================================================


@bot.tree.command(
    name='muteall', description='Muta todos os membros na sua call.'
)
@app_commands.checks.has_permissions(mute_members=True)
async def muteall(interaction: discord.Interaction):
  await interaction.response.defer()
  member = interaction.guild.get_member(
      interaction.user.id
  ) or await interaction.guild.fetch_member(interaction.user.id)
  if not member or not member.voice or not member.voice.channel:
    await interaction.followup.send(
        '❌ Você precisa estar em um canal de voz!', ephemeral=True
    )
    return

  channel = member.voice.channel
  count = 0
  for m in channel.members:
    try:
      await m.edit(mute=True)
      count += 1
    except discord.Forbidden:
      pass

  await interaction.followup.send(
      f'🔇 **{count}** membros mutados em **{channel.name}**!'
  )


@bot.tree.command(
    name='unmuteall', description='Desmuta todos os membros na sua call.'
)
@app_commands.checks.has_permissions(mute_members=True)
async def unmuteall(interaction: discord.Interaction):
  await interaction.response.defer()
  member = interaction.guild.get_member(
      interaction.user.id
  ) or await interaction.guild.fetch_member(interaction.user.id)
  if not member or not member.voice or not member.voice.channel:
    await interaction.followup.send(
        '❌ Você precisa estar em um canal de voz!', ephemeral=True
    )
    return

  channel = member.voice.channel
  count = 0
  for m in channel.members:
    try:
      await m.edit(mute=False)
      count += 1
    except discord.Forbidden:
      pass

  await interaction.followup.send(
      f'🔊 **{count}** membros desmutados em **{channel.name}**!'
  )


# ==============================================================================
# 💞 INTERAÇÃO SOCIAL & GIFS DETALHADOS
# ==============================================================================


@bot.tree.command(name='abracar', description='Envia um abraço carinhoso.')
@app_commands.describe(membro='Membro para abraçar')
async def abracar(interaction: discord.Interaction, membro: discord.Member):
  await interaction.response.defer()
  gif_url = await get_anime_gif('hug')
  embed = discord.Embed(
      description=(
          f'🤗 {interaction.user.mention} deu um abraço carinhoso em'
          f' {membro.mention}!'
      ),
      color=discord.Color.pink(),
  )
  if gif_url:
    embed.set_image(url=gif_url)
  await interaction.followup.send(embed=embed)


@bot.tree.command(name='beijar', description='Envia um beijo apaixonado.')
@app_commands.describe(membro='Membro para beijar')
async def beijar(interaction: discord.Interaction, membro: discord.Member):
  await interaction.response.defer()
  gif_url = await get_anime_gif('kiss')
  embed = discord.Embed(
      description=(
          f'💋 {interaction.user.mention} deu um beijo apaixonado em'
          f' {membro.mention}!'
      ),
      color=discord.Color.red(),
  )
  if gif_url:
    embed.set_image(url=gif_url)
  await interaction.followup.send(embed=embed)


@bot.tree.command(name='tapa', description='Dá um tapa de brincadeira.')
@app_commands.describe(membro='Membro para dar um tapa')
async def tapa(interaction: discord.Interaction, membro: discord.Member):
  await interaction.response.defer()
  gif_url = await get_anime_gif('slap')
  embed = discord.Embed(
      description=(
          f'🖐️ {interaction.user.mention} deu um tapa em {membro.mention}!'
      ),
      color=discord.Color.orange(),
  )
  if gif_url:
    embed.set_image(url=gif_url)
  await interaction.followup.send(embed=embed)


@bot.tree.command(
    name='cafune', description='Faz um carinho na cabeça do membro.'
)
@app_commands.describe(membro='Membro para fazer carinho')
async def cafune(interaction: discord.Interaction, membro: discord.Member):
  await interaction.response.defer()
  gif_url = await get_anime_gif('pat')
  embed = discord.Embed(
      description=(
          f'🫳 {interaction.user.mention} fez carinho na cabeça de'
          f' {membro.mention}!'
      ),
      color=discord.Color.purple(),
  )
  if gif_url:
    embed.set_image(url=gif_url)
  await interaction.followup.send(embed=embed)


@bot.tree.command(
    name='cutucar', description='Cutuca um membro para chamar atenção.'
)
@app_commands.describe(membro='Membro para cutucar')
async def cutucar(interaction: discord.Interaction, membro: discord.Member):
  await interaction.response.defer()
  gif_url = await get_anime_gif('poke')
  embed = discord.Embed(
      description=f'👉 {interaction.user.mention} cutucou {membro.mention}!',
      color=discord.Color.light_grey(),
  )
  if gif_url:
    embed.set_image(url=gif_url)
  await interaction.followup.send(embed=embed)


@bot.tree.command(name='tocaaqui', description='Manda um Toca Aqui (High Five).')
@app_commands.describe(membro='Membro para mandar o toca aqui')
async def tocaaqui(interaction: discord.Interaction, membro: discord.Member):
  await interaction.response.defer()
  gif_url = await get_anime_gif('highfive')
  embed = discord.Embed(
      description=(
          f'🖐️ {interaction.user.mention} mandou um toca aqui para'
          f' {membro.mention}!'
      ),
      color=discord.Color.gold(),
  )
  if gif_url:
    embed.set_image(url=gif_url)
  await interaction.followup.send(embed=embed)


@bot.tree.command(name='morder', description='Dá uma mordida engraçada.')
@app_commands.describe(membro='Membro para morder')
async def morder(interaction: discord.Interaction, membro: discord.Member):
  await interaction.response.defer()
  gif_url = await get_anime_gif('bite')
  embed = discord.Embed(
      description=(
          f'🦷 {interaction.user.mention} deu uma mordida em {membro.mention}!'
      ),
      color=discord.Color.dark_red(),
  )
  if gif_url:
    embed.set_image(url=gif_url)
  await interaction.followup.send(embed=embed)


@bot.tree.command(name='cosquinhas', description='Faz cosquinhas em alguém.')
@app_commands.describe(membro='Membro para fazer cosquinhas')
async def cosquinhas(interaction: discord.Interaction, membro: discord.Member):
  await interaction.response.defer()
  gif_url = await get_anime_gif('tickle')
  embed = discord.Embed(
      description=(
          f'🤏 {interaction.user.mention} fez cosquinhas em {membro.mention}!'
      ),
      color=discord.Color.yellow(),
  )
  if gif_url:
    embed.set_image(url=gif_url)
  await interaction.followup.send(embed=embed)


@bot.tree.command(
    name='ship', description='Calcula a compatibilidade do casal.'
)
@app_commands.describe(
    membro1='Primeira pessoa', membro2='Segunda pessoa'
)
async def ship(
    interaction: discord.Interaction,
    membro1: discord.Member,
    membro2: discord.Member,
):
  porcentagem = random.randint(0, 100)
  nome1 = membro1.display_name
  nome2 = membro2.display_name
  ship_name = nome1[: len(nome1) // 2] + nome2[len(nome2) // 2 :]

  blocos = int(porcentagem / 10)
  barra = '█' * blocos + '░' * (10 - blocos)

  if porcentagem > 80:
    msg = '💖 Combinação perfeita! Quando é o casamento?'
  elif porcentagem > 50:
    msg = '😊 Há uma química real aqui!'
  elif porcentagem > 20:
    msg = '😅 Melhores como amigos...'
  else:
    msg = '💔 Totalmente incompatíveis.'

  embed = discord.Embed(
      title=f'💕 Calculadora do Amor: {ship_name}',
      description=(
          f'**{membro1.display_name}** +'
          f' **{membro2.display_name}**\n\n**Afinidade:**'
          f' `{porcentagem}%`\n[{barra}]\n\n_{msg}_'
      ),
      color=discord.Color.magenta(),
  )
  await interaction.response.send_message(embed=embed)


# ==============================================================================
# 🎮 JOGOS & DIVERSÃO
# ==============================================================================


@bot.tree.command(
    name='jokempo', description='Desafie o bot no Pedra, Papel ou Tesoura.'
)
@app_commands.choices(
    opcao=[
        app_commands.Choice(name='Pedra 🪨', value='pedra'),
        app_commands.Choice(name='Papel 📄', value='papel'),
        app_commands.Choice(name='Tesoura ✂️', value='tesoura'),
    ]
)
async def jokempo(
    interaction: discord.Interaction, opcao: app_commands.Choice[str]
):
  escolha_usuario = opcao.value
  escolha_bot = random.choice(['pedra', 'papel', 'tesoura'])
  emojis = {'pedra': '🪨', 'papel': '📄', 'tesoura': '✂️'}

  if escolha_usuario == escolha_bot:
    resultado = '🤝 **Empate!**'
    cor = discord.Color.gold()
  elif (
      (escolha_usuario == 'pedra' and escolha_bot == 'tesoura')
      or (escolha_usuario == 'papel' and escolha_bot == 'pedra')
      or (escolha_usuario == 'tesoura' and escolha_bot == 'papel')
  ):
    resultado = '🎉 **Você Venceu!**'
    cor = discord.Color.green()
  else:
    resultado = '🤖 **Eu Venci!**'
    cor = discord.Color.red()

  embed = discord.Embed(title='🎮 Jo-Ken-Pô', description=resultado, color=cor)
  embed.add_field(
      name='Você',
      value=f'{emojis[escolha_usuario]} {escolha_usuario.capitalize()}',
      inline=True,
  )
  embed.add_field(
      name='Bot',
      value=f'{emojis[escolha_bot]} {escolha_bot.capitalize()}',
      inline=True,
  )
  await interaction.response.send_message(embed=embed)


@bot.tree.command(
    name='adivinhar', description='Adivinhe o número secreto de 1 a 10.'
)
@app_commands.describe(numero='Seu palpite (1-10)')
async def adivinhar(interaction: discord.Interaction, numero: int):
  if numero < 1 or numero > 10:
    await interaction.response.send_message(
        '❌ Digite um número válido entre 1 e 10!', ephemeral=True
    )
    return

  secreto = random.randint(1, 10)
  if numero == secreto:
    await interaction.response.send_message(
        f'🎯 **Acertou em cheio!** O número secreto era **{secreto}**!'
    )
  else:
    await interaction.response.send_message(
        f'❌ **Errou!** Você escolheu **{numero}**, mas o correto era'
        f' **{secreto}**.'
    )


@bot.tree.command(
    name='8ball', description='Pergunte algo à Bola Mágica do Destino.'
)
@app_commands.describe(pergunta='Sua dúvida')
async def eightball(interaction: discord.Interaction, pergunta: str):
  respostas = [
      'Com certeza sim! ✨',
      'Sem dúvidas! 👍',
      'Minhas fontes dizem que sim 🔮',
      'Acho melhor não te responder agora... 😶',
      'Minha resposta é não ❌',
      'Pouco provável 📉',
  ]
  embed = discord.Embed(
      title='🔮 Bola Mágica 8-Ball', color=discord.Color.dark_purple()
  )
  embed.add_field(name='❓ Pergunta:', value=pergunta, inline=False)
  embed.add_field(
      name='💬 Resposta:', value=random.choice(respostas), inline=False
  )
  await interaction.response.send_message(embed=embed)


@bot.tree.command(name='dado', description='Rola um dado de quantos lados quiser.')
@app_commands.describe(lados='Lados do dado (Padrão: 6)')
async def dado(interaction: discord.Interaction, lados: int = 6):
  if lados < 2:
    await interaction.response.send_message(
        '❌ O dado precisa ter pelo menos 2 lados!', ephemeral=True
    )
    return
  resultado = random.randint(1, lados)
  await interaction.response.send_message(
      f'🎲 Você rolou um dado de **{lados} lados** e obteve: **{resultado}**!'
  )


@bot.tree.command(name='moeda', description='Joga uma moeda: Cara ou Coroa.')
async def moeda(interaction: discord.Interaction):
  resultado = random.choice(['Cara 🪙', 'Coroa 👑'])
  await interaction.response.send_message(
      f'🪙 A moeda caiu em: **{resultado}**!'
  )


@bot.tree.command(
    name='escolher', description='Deixe o bot escolher uma opção por você.'
)
@app_commands.describe(opcoes='Opções separadas por vírgula')
async def escolher(interaction: discord.Interaction, opcoes: str):
  lista = [op.strip() for op in opcoes.split(',') if op.strip()]
  if len(lista) < 2:
    await interaction.response.send_message(
        '❌ Insira pelo menos duas opções separadas por vírgula!', ephemeral=True
    )
    return
  escolhido = random.choice(lista)
  await interaction.response.send_message(
      f'🤔 Minha escolha é: **{escolhido}**!'
  )


# ==============================================================================
# 🎨 UTILITÁRIOS & FERRAMENTAS
# ==============================================================================


@bot.tree.command(
    name='conquista', description='Gera conquista estilo Minecraft.'
)
@app_commands.describe(texto='Texto da conquista')
async def conquista(interaction: discord.Interaction, texto: str):
  await interaction.response.defer()
  texto_enc = urllib.parse.quote(texto)
  url_conquista = (
      'https://mcgen.herokuapp.com/a.php?i=1&h=Conquista+Realizada%21&t='
      f'{texto_enc}'
  )

  embed = discord.Embed(
      title='🏆 Nova Conquista Desbloqueada!', color=discord.Color.green()
  )
  embed.set_image(url=url_conquista)
  await interaction.followup.send(embed=embed)


@bot.tree.command(name='avatar', description='Exibe a foto de perfil em HD.')
@app_commands.describe(membro='Membro para ver a foto')
async def avatar(
    interaction: discord.Interaction, membro: discord.Member = None
):
  alvo = membro or interaction.user
  avatar_url = alvo.display_avatar.url

  embed = discord.Embed(
      title=f'🖼️ Avatar de {alvo.display_name}', color=discord.Color.blurple()
  )
  embed.set_image(url=avatar_url)
  embed.add_field(name='🔗 Link Direto', value=f'[Baixar em HD]({avatar_url})')
  await interaction.response.send_message(embed=embed)


@bot.tree.command(
    name='image', description='Gera imagens usando Inteligência Artificial.'
)
@app_commands.describe(prompt='Descrição detalhada da imagem')
async def image(interaction: discord.Interaction, prompt: str):
  await interaction.response.defer()
  encoded_prompt = urllib.parse.quote(
      f'{prompt}, high quality, realistic, detailed, 8k resolution'
  )
  image_url = (
      f'https://pollinations.ai/p/{encoded_prompt}?width=1024&height=1024&seed=100&nologo=true'
  )

  headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
  try:
    async with aiohttp.ClientSession(headers=headers) as session:
      async with session.get(image_url, timeout=35) as resp:
        if resp.status == 200:
          data = await resp.read()
          file = discord.File(io.BytesIO(data), filename='imagem.png')
          embed = discord.Embed(
              title='🎨 Arte Gerada por IA',
              description=f'**Prompt:** `{prompt}`',
              color=discord.Color.purple(),
          )
          embed.set_image(url='attachment://imagem.png')
          embed.set_footer(text='Gerado via Pollinations Engine')
          await interaction.followup.send(embed=embed, file=file)
        else:
          await interaction.followup.send(
              '❌ Falha ao conectar ao motor de IA.'
          )
  except Exception:
    await interaction.followup.send('❌ Erro no processamento da imagem.')


@bot.tree.command(
    name='meme', description='Busca memes ou cria um meme customizado.'
)
@app_commands.describe(
    descricao='Texto do meme', imagem='Imagem opcional'
)
async def meme(
    interaction: discord.Interaction,
    descricao: str,
    imagem: discord.Attachment = None,
):
  await interaction.response.defer()
  if imagem:
    embed = discord.Embed(
        title='🎭 Meme Personalizado',
        description=f'**{descricao}**',
        color=discord.Color.gold(),
    )
    embed.set_image(url=imagem.url)
    await interaction.followup.send(embed=embed)
    return

  async with aiohttp.ClientSession() as session:
    try:
      async with session.get(
          'https://meme-api.com/gimme', timeout=10
      ) as response:
        if response.status == 200:
          data = await response.json()
          embed = discord.Embed(
              title=f"🎭 {data.get('title', 'Meme')}",
              description=f'**Legenda:** {descricao}',
              color=discord.Color.gold(),
          )
          embed.set_image(url=data['url'])
          await interaction.followup.send(embed=embed)
        else:
          await interaction.followup.send('❌ Falha ao buscar meme da web.')
    except Exception:
      await interaction.followup.send('❌ Erro de conexão com a API.')


@bot.tree.command(name='calcule', description='Calculadora rápida de expressões.')
@app_commands.describe(expressao='Ex: (10 + 5) * 2 / 3')
async def calcule(interaction: discord.Interaction, expressao: str):
  await interaction.response.defer()
  query = urllib.parse.quote(expressao)
  async with aiohttp.ClientSession() as session:
    try:
      async with session.get(
          f'https://api.mathjs.org/v4/?expr={query}'
      ) as response:
        if response.status == 200:
          resultado = await response.text()
          embed = discord.Embed(
              title='🧮 Calculadora Rápida', color=discord.Color.green()
          )
          embed.add_field(
              name='Expressão', value=f'`{expressao}`', inline=False
          )
          embed.add_field(
              name='Resultado', value=f'`{resultado}`', inline=False
          )
          await interaction.followup.send(embed=embed)
        else:
          await interaction.followup.send('❌ Expressão matemática inválida.')
    except Exception:
      await interaction.followup.send('❌ Erro ao calcular.')


# ==============================================================================
# 🚨 TRATAMENTO DE PERMISSÕES ADMINISTRATIVAS DE ÁUDIO
# ==============================================================================


@recomecar.error
@reiniciarfila.error
@skip.error
@stop.error
@muteall.error
@unmuteall.error
async def permissions_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
):
  if isinstance(error, app_commands.MissingPermissions):
    msg = (
        '⛔ **Acesso Negado!** Você precisa de permissões administrativas para'
        ' usar este comando.'
    )
    if interaction.response.is_done():
      await interaction.followup.send(msg, ephemeral=True)
    else:
      await interaction.response.send_message(msg, ephemeral=True)


# ==============================================================================
# 🚀 INICIALIZAÇÃO
# ==============================================================================

import os
bot.run(os.environ.get('MTU1MTcxMTIxOTc2Mzc3NzU0Ng.GOK8z2.iRNY6ntGVw0sIFbaQ71Id87i2GKRtB_LDTrUDA')
