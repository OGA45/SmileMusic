"""LT (Lightning Talk) deck generator for the SmileMusic Discord bot.

Run: python activity/build_lt_slides.py
Output: activity/LT_SmileMusic.pptx
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.util import Inches, Pt, Emu

from pygments import lex
from pygments.lexers.python import PythonLexer
from pygments.lexers.data import YamlLexer
from pygments.token import Token


BG_DARK = RGBColor(0x0F, 0x14, 0x1F)
BG_PANEL = RGBColor(0x18, 0x21, 0x33)
ACCENT = RGBColor(0x5E, 0xE0, 0xC1)
ACCENT2 = RGBColor(0xFF, 0xC4, 0x6B)
TEXT_MAIN = RGBColor(0xF2, 0xF5, 0xFA)
TEXT_SUB = RGBColor(0xA9, 0xB3, 0xC7)
CODE_BG = RGBColor(0x0B, 0x10, 0x18)

JP_FONT = "Yu Gothic UI"
CODE_FONT = "Consolas"

# --- editor window chrome ---
WIN_BG = RGBColor(0x10, 0x16, 0x22)         # code area
WIN_TITLE_BG = RGBColor(0x1B, 0x22, 0x30)   # title bar
WIN_BORDER = RGBColor(0x2A, 0x33, 0x46)
WIN_TITLE_FG = RGBColor(0xB6, 0xBD, 0xCB)
LINE_NUM_FG = RGBColor(0x4C, 0x55, 0x6A)

TRAFFIC_RED = RGBColor(0xFF, 0x5F, 0x57)
TRAFFIC_YELLOW = RGBColor(0xFE, 0xBC, 0x2E)
TRAFFIC_GREEN = RGBColor(0x28, 0xC8, 0x40)

# --- syntax highlight palette (One Dark inspired) ---
SH_KEY = RGBColor(0xC6, 0x78, 0xDD)        # keywords / operator words
SH_OP = RGBColor(0xE0, 0x6C, 0x75)         # operators / self
SH_BUILTIN = RGBColor(0x56, 0xB6, 0xC2)    # builtins (True/False/None/print...)
SH_FUNC = RGBColor(0x61, 0xAF, 0xEF)       # function names (def f)
SH_TYPE = RGBColor(0xE5, 0xC0, 0x7B)       # class / decorator / exception
SH_STR = RGBColor(0x98, 0xC3, 0x79)        # strings
SH_NUM = RGBColor(0xD1, 0x9A, 0x66)        # numbers / constants
SH_COMMENT = RGBColor(0x7F, 0x88, 0x9B)    # comments (italic)
SH_TAG = RGBColor(0x5E, 0xE0, 0xC1)        # YAML keys etc.

TOKEN_COLORS = {
    Token.Keyword: SH_KEY,
    Token.Keyword.Constant: SH_NUM,
    Token.Operator.Word: SH_KEY,
    Token.Operator: SH_OP,
    Token.Name.Builtin: SH_BUILTIN,
    Token.Name.Builtin.Pseudo: SH_OP,            # self / cls
    Token.Name.Function: SH_FUNC,
    Token.Name.Function.Magic: SH_FUNC,
    Token.Name.Class: SH_TYPE,
    Token.Name.Decorator: SH_TYPE,
    Token.Name.Exception: SH_TYPE,
    Token.Name.Tag: SH_TAG,                      # YAML keys, HTML tags
    Token.Literal.String: SH_STR,
    Token.Literal.String.Affix: SH_KEY,          # f / r / b prefixes
    Token.Literal.String.Interpol: SH_TYPE,      # {expr} inside f-string
    Token.Literal.String.Escape: SH_NUM,
    Token.Literal.Number: SH_NUM,
    Token.Literal.Scalar: SH_STR,                # YAML plain scalar values
    Token.Comment: SH_COMMENT,
}

PY_LEXER = PythonLexer(stripnl=False, ensurenl=False)
YAML_LEXER = YamlLexer(stripnl=False, ensurenl=False)
LEXERS = {"python": PY_LEXER, "yaml": YAML_LEXER}


def color_for_token(ttype):
    t = ttype
    while t is not None:
        if t in TOKEN_COLORS:
            return TOKEN_COLORS[t]
        t = t.parent
    return TEXT_MAIN


def is_comment_token(ttype):
    t = ttype
    while t is not None:
        if t is Token.Comment:
            return True
        t = t.parent
    return False

SLIDE_W = Inches(13.333)
SLIDE_H = Inches(7.5)


@dataclass
class Slide:
    title: str
    subtitle: str = ""
    bullets: list[str] = field(default_factory=list)
    code: str | None = None
    lang: str = "python"
    code_title: str = ""  # window title (filename). 空なら lang から推定
    note: str = ""
    layout: str = "content"  # content | title | section | finale


def fill_solid(shape, color):
    fill = shape.fill
    fill.solid()
    fill.fore_color.rgb = color
    shape.line.fill.background()


def add_background(slide, color=BG_DARK):
    rect = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, 0, SLIDE_W, SLIDE_H)
    fill_solid(rect, color)
    rect.shadow.inherit = False
    return rect


def add_text(
    slide,
    text,
    *,
    left,
    top,
    width,
    height,
    size=18,
    bold=False,
    color=TEXT_MAIN,
    font=JP_FONT,
    align=PP_ALIGN.LEFT,
    anchor=MSO_ANCHOR.TOP,
):
    tb = slide.shapes.add_textbox(left, top, width, height)
    tf = tb.text_frame
    tf.word_wrap = True
    tf.margin_left = Emu(0)
    tf.margin_right = Emu(0)
    tf.margin_top = Emu(0)
    tf.margin_bottom = Emu(0)
    tf.vertical_anchor = anchor

    lines = text.split("\n") if isinstance(text, str) else text
    for i, line in enumerate(lines):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align
        run = p.add_run()
        run.text = line
        run.font.name = font
        run.font.size = Pt(size)
        run.font.bold = bold
        run.font.color.rgb = color
    return tb


def add_accent_bar(slide, left, top, width=Inches(0.18), height=Inches(0.55)):
    bar = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, left, top, width, height)
    fill_solid(bar, ACCENT)
    return bar


def add_panel(slide, left, top, width, height, color=BG_PANEL):
    panel = slide.shapes.add_shape(
        MSO_SHAPE.ROUND_SAME_SIDE_RECTANGLE, left, top, width, height
    )
    fill_solid(panel, color)
    panel.shadow.inherit = False
    return panel


DEFAULT_CODE_TITLES = {
    "python": "smile_music3.py",
    "yaml": "docker-compose.yml",
}

CODE_FONT_PT = 11
CODE_LINE_PT = 14  # 固定行高: gutter と本文の縦位置を揃える


def add_code_block(slide, code, *, left, top, width, height,
                   lang="python", code_title=""):
    """エディタ風ウィンドウとして描画する。

    レイアウト:
      [TITLE BAR] (高さ 0.36in, traffic lights + filename)
      [GUTTER]  [CODE]
    """
    title = code_title or DEFAULT_CODE_TITLES.get(lang, "code")
    code = code.rstrip("\n")  # 末尾の余分な改行は段落数を狂わせるので落とす

    # ----- ウィンドウ外枠 -----
    win = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, left, top, width, height)
    fill_solid(win, WIN_BG)
    win.line.color.rgb = WIN_BORDER
    win.line.width = Pt(0.75)

    # ----- タイトルバー -----
    bar_h = Inches(0.36)
    bar = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, left, top, width, bar_h)
    fill_solid(bar, WIN_TITLE_BG)
    bar.line.fill.background()

    # ----- トラフィックライト -----
    light_size = Inches(0.13)
    light_y = top + (bar_h - light_size) // 2
    light_left = left + Inches(0.13)
    gap = Inches(0.07)
    for color in (TRAFFIC_RED, TRAFFIC_YELLOW, TRAFFIC_GREEN):
        c = slide.shapes.add_shape(
            MSO_SHAPE.OVAL, light_left, light_y, light_size, light_size,
        )
        fill_solid(c, color)
        c.line.fill.background()
        light_left += light_size + gap

    # ----- ファイル名(タブ風に中央表示) -----
    title_box = slide.shapes.add_textbox(
        left + Inches(0.65), top, width - Inches(1.3), bar_h,
    )
    tf = title_box.text_frame
    tf.margin_left = Emu(0); tf.margin_right = Emu(0)
    tf.margin_top = Emu(0); tf.margin_bottom = Emu(0)
    tf.vertical_anchor = MSO_ANCHOR.MIDDLE
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER
    p.space_before = Pt(0); p.space_after = Pt(0)
    run = p.add_run()
    run.text = title
    run.font.name = JP_FONT
    run.font.size = Pt(10)
    run.font.color.rgb = WIN_TITLE_FG

    # ----- コード領域(行番号 + 本文) -----
    code_top = top + bar_h
    inner_top = code_top + Inches(0.12)
    inner_bottom = top + height - Inches(0.12)
    inner_h = inner_bottom - inner_top

    gutter_w = Inches(0.40)
    gutter_left = left + Inches(0.16)
    code_left = gutter_left + gutter_w + Inches(0.14)
    code_right = left + width - Inches(0.16)
    code_w = code_right - code_left

    n_lines = code.count("\n") + 1

    # gutter
    gtb = slide.shapes.add_textbox(gutter_left, inner_top, gutter_w, inner_h)
    gtf = gtb.text_frame
    gtf.word_wrap = False
    gtf.margin_left = Emu(0); gtf.margin_right = Emu(0)
    gtf.margin_top = Emu(0); gtf.margin_bottom = Emu(0)
    for i in range(n_lines):
        p = gtf.paragraphs[0] if i == 0 else gtf.add_paragraph()
        p.alignment = PP_ALIGN.RIGHT
        p.space_before = Pt(0); p.space_after = Pt(0)
        p.line_spacing = Pt(CODE_LINE_PT)
        run = p.add_run()
        run.text = str(i + 1)
        run.font.name = CODE_FONT
        run.font.size = Pt(CODE_FONT_PT)
        run.font.color.rgb = LINE_NUM_FG

    # code 本文
    tb = slide.shapes.add_textbox(code_left, inner_top, code_w, inner_h)
    tf = tb.text_frame
    tf.word_wrap = False
    tf.margin_left = Emu(0); tf.margin_right = Emu(0)
    tf.margin_top = Emu(0); tf.margin_bottom = Emu(0)

    def _setup(p):
        p.alignment = PP_ALIGN.LEFT
        p.space_before = Pt(0)
        p.space_after = Pt(0)
        p.line_spacing = Pt(CODE_LINE_PT)

    p = tf.paragraphs[0]
    _setup(p)

    lexer = LEXERS.get(lang, PY_LEXER)
    for ttype, value in lex(code, lexer):
        if not value:
            continue
        color = color_for_token(ttype)
        italic = is_comment_token(ttype)
        parts = value.split("\n")
        for i, part in enumerate(parts):
            if i > 0:
                p = tf.add_paragraph()
                _setup(p)
            if not part:
                continue
            run = p.add_run()
            run.text = part
            run.font.name = CODE_FONT
            run.font.size = Pt(CODE_FONT_PT)
            run.font.color.rgb = color
            if italic:
                run.font.italic = True


def add_footer(slide, idx, total):
    add_text(
        slide,
        "SmileMusic — Discord Music Bot deep-dive",
        left=Inches(0.5), top=Inches(7.05), width=Inches(8), height=Inches(0.35),
        size=10, color=TEXT_SUB,
    )
    add_text(
        slide,
        f"{idx:02d} / {total:02d}",
        left=Inches(11.8), top=Inches(7.05), width=Inches(1.05), height=Inches(0.35),
        size=10, color=TEXT_SUB, align=PP_ALIGN.RIGHT,
    )


def render_title_slide(slide, s: Slide):
    add_background(slide)
    # 装飾の縦バー
    bar = slide.shapes.add_shape(
        MSO_SHAPE.RECTANGLE, Inches(0.6), Inches(2.4), Inches(0.18), Inches(2.7)
    )
    fill_solid(bar, ACCENT)

    add_text(
        slide, s.title,
        left=Inches(0.95), top=Inches(2.3), width=Inches(11.5), height=Inches(1.6),
        size=46, bold=True, color=TEXT_MAIN,
    )
    if s.subtitle:
        add_text(
            slide, s.subtitle,
            left=Inches(0.95), top=Inches(3.95), width=Inches(11.5), height=Inches(0.9),
            size=22, color=ACCENT,
        )
    add_text(
        slide, s.note,
        left=Inches(0.95), top=Inches(5.0), width=Inches(11.5), height=Inches(1.2),
        size=14, color=TEXT_SUB,
    )


def render_section_slide(slide, s: Slide):
    add_background(slide, BG_PANEL)
    add_text(
        slide, s.subtitle or "SECTION",
        left=Inches(0.9), top=Inches(2.6), width=Inches(11.5), height=Inches(0.6),
        size=16, color=ACCENT, bold=True,
    )
    add_text(
        slide, s.title,
        left=Inches(0.9), top=Inches(3.2), width=Inches(11.5), height=Inches(2.0),
        size=44, bold=True, color=TEXT_MAIN,
    )


def render_content_slide(slide, s: Slide):
    add_background(slide)
    add_accent_bar(slide, Inches(0.55), Inches(0.55))
    add_text(
        slide, s.title,
        left=Inches(0.85), top=Inches(0.45), width=Inches(11.8), height=Inches(0.7),
        size=30, bold=True,
    )
    if s.subtitle:
        add_text(
            slide, s.subtitle,
            left=Inches(0.85), top=Inches(1.05), width=Inches(11.8), height=Inches(0.45),
            size=15, color=ACCENT,
        )

    bullet_top = Inches(1.65)
    bullet_left = Inches(0.85)
    bullet_width = Inches(11.8) if not s.code else Inches(5.6)
    if s.bullets:
        tb = slide.shapes.add_textbox(bullet_left, bullet_top, bullet_width, Inches(5.0))
        tf = tb.text_frame
        tf.word_wrap = True
        tf.margin_left = Emu(0)
        tf.margin_top = Emu(0)
        for i, b in enumerate(s.bullets):
            p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            p.alignment = PP_ALIGN.LEFT
            p.space_after = Pt(8)

            mark_run = p.add_run()
            mark_run.text = "▍ "
            mark_run.font.name = JP_FONT
            mark_run.font.size = Pt(18)
            mark_run.font.color.rgb = ACCENT
            mark_run.font.bold = True

            run = p.add_run()
            run.text = b
            run.font.name = JP_FONT
            run.font.size = Pt(18)
            run.font.color.rgb = TEXT_MAIN
            run.font.bold = False

    if s.code:
        add_code_block(
            slide, s.code,
            left=Inches(6.7), top=Inches(1.65),
            width=Inches(6.1), height=Inches(4.9),
            lang=s.lang,
            code_title=s.code_title,
        )

    if s.note:
        add_text(
            slide, s.note,
            left=Inches(0.85), top=Inches(6.55), width=Inches(11.8), height=Inches(0.45),
            size=12, color=TEXT_SUB,
        )


def render_finale_slide(slide, s: Slide):
    add_background(slide)
    add_text(
        slide, s.title,
        left=Inches(0.9), top=Inches(2.5), width=Inches(11.5), height=Inches(1.4),
        size=44, bold=True, color=TEXT_MAIN,
    )
    add_text(
        slide, s.subtitle,
        left=Inches(0.9), top=Inches(3.9), width=Inches(11.5), height=Inches(0.8),
        size=22, color=ACCENT,
    )
    add_text(
        slide, s.note,
        left=Inches(0.9), top=Inches(4.9), width=Inches(11.5), height=Inches(1.6),
        size=14, color=TEXT_SUB,
    )


RENDERERS = {
    "title": render_title_slide,
    "section": render_section_slide,
    "content": render_content_slide,
    "finale": render_finale_slide,
}


SLIDES: list[Slide] = [
    Slide(
        layout="title",
        title="Discord 音楽BOTを fork して\nffmpeg と仲良くなった話",
        subtitle="SmileMusic — Lightning Talk (5分)",
        note="技術者向け / 発表者: 男鹿  /  Repo: github.com/OGA45/SmileMusic",
    ),
    Slide(
        layout="content",
        title="自己紹介と今日のゴール",
        subtitle="WHO / WHY",
        bullets=[
            "業務はWeb寄り、最近は社内ツールやBOTを書いている",
            "akomekagome/SmileMusic を fork して欲しい機能を生やしている",
            "今日は『discord.py の高レベルAPI を一度剥がして遊ぶ』話",
            "持って帰ってほしい: ffmpeg/Opus/yt-dlp の少し下のレイヤ",
        ],
        note="自己紹介はあっさり。ゴールを宣言してすぐ本題へ。",
    ),
    Slide(
        layout="content",
        title="SmileMusic とは",
        subtitle="WHAT — 30秒で機能紹介",
        bullets=[
            "Discord スラッシュコマンドで動く音楽再生BOT",
            "/play /list /live /seek /rewind /loop /queue /now /histry ...",
            "ソース: YouTube / niconico / Spotify / Jellyfin / 任意URL (yt-dlp)",
            "永続化: PostgreSQL — guildごとに volume/stream/prefix と再生履歴",
            "Docker 一発で起動: Python 3.14 + libopus + ffmpeg + deno",
        ],
        code=(
            "services:\n"
            "  smile_music_db:\n"
            "    image: postgres:13.2\n"
            "  smile_music_py3:\n"
            "    build: .\n"
            "    command: python smile_music3.py\n"
            "    depends_on:\n"
            "      smile_music_db:\n"
            "        condition: service_healthy\n"
        ),
        lang="yaml",
        note="docker-compose.yml の抜粋。3つ目のBOT (py3) が現役。",
    ),
    Slide(
        layout="section",
        title="discord.py の音声まわりは\n意外と低レイヤを触れる",
        subtitle="DEEP DIVE",
    ),
    Slide(
        layout="content",
        title="工夫① Opus パケットの passthrough",
        subtitle="再エンコードを避けて CPU を返してもらう",
        bullets=[
            "ffmpeg の probe で codec を判定 — opus なら -c:a copy",
            "FFmpegOpusAudio を継承して、コーデック自動選択を上書き",
            "Discord Voice は 48kHz/2ch/Opus が前提なので、入力が opus ならそのまま流せる",
            "効果: 多人数サーバーで複数BOT同時稼働してもCPUに余裕",
        ],
        code=(
            "probe = await discord.FFmpegOpusAudio.probe(source)\n"
            "if probe[0] == 'opus':\n"
            "    return OriginalFFmpegOpusAudio(source, **opts)\n"
            "else:\n"
            "    return OriginalFFmpegPCMAudio(source, **opts)\n"
            "\n"
            "# OriginalFFmpegOpusAudio.__init__\n"
            "self._codec = (\n"
            "    'copy'\n"
            "    if codec in ('opus', 'libopus', 'copy')\n"
            "    else 'libopus'\n"
            ")\n"
        ),
        note="copy が決まる経路を抑えるのがポイント。",
    ),
    Slide(
        layout="content",
        title="工夫② seek / rewind を自前実装",
        subtitle="ffmpeg プロセスを kill→spawn する",
        bullets=[
            "discord.py 標準には seek が無い (ストリームを読み続けるだけ)",
            "実装: -ss <seek_time> を付けた ffmpeg を spawn し、旧プロセスを kill",
            "再生位置は read() を override して 20ms フレーム単位で自前カウント",
            "ハマり: PCM 経路の -f s16le と Opus 経路の -f opus を取り違えると即EOF",
            "Interaction の 3秒応答制限超え対策で defer → followup で結果通知",
        ],
        code=(
            "def read(self):\n"
            "    ret = super().read()\n"
            "    if ret:\n"
            "        self.total_milliseconds += 20  # 1 frame\n"
            "    return ret\n"
            "\n"
            "def seek(self, seek_time, **kw):\n"
            "    self.total_milliseconds = (\n"
            "        self.get_tootal_millisecond(seek_time))\n"
            "    args = ['ffmpeg', '-ss', seek_time,\n"
            "            '-i', self.source,\n"
            "            '-f', 'opus', '-c:a', self._codec,\n"
            "            '-ar', '48000', '-ac', '2', 'pipe:1']\n"
            "    self._process = self._spawn_process(args, ...)\n"
            "    self._packet_iter = OggStream(\n"
            "        self._stdout).iter_packets()\n"
            "    self.kill(old_proc)\n"
        ),
        note="OggStream をそのまま使い続けるのがミソ。",
    ),
    Slide(
        layout="content",
        title="工夫③ niconico だけ別経路",
        subtitle="独自fork の niconico_dl_async + ローカルキャッシュ",
        bullets=[
            "ニコニコは heartbeat を打ち続けないとセッションが切れる",
            "tasuren/niconico_dl_async を取り込んで、aiohttp で非同期化",
            "ダウンロード済みなら sm***.opus を直接再生(2回目以降は爆速)",
            "進捗は yt-dlp の progress_hooks → asyncio.run_coroutine_threadsafe で Embed を更新",
        ],
        code=(
            "match = re.search(r'/watch/(sm[0-9]+)', url)\n"
            "if match:\n"
            "    file_name = f'{match.group(1)}.opus'\n"
            "    if os.path.exists(file_name):\n"
            "        return OriginalFFmpegOpusAudio(\n"
            "            file_name, **cache_option)\n"
            "    _data = await _download_with_progress(\n"
            "        ctx, url, loop)\n"
            "\n"
            "# 別スレッドのhookからイベントループに戻す\n"
            "asyncio.run_coroutine_threadsafe(\n"
            "    progress_msg.edit(embed=new_embed), loop)\n"
        ),
        note="スレッド境界を超える Embed 更新が地味に効く。",
    ),
    Slide(
        layout="content",
        title="工夫④ YouTube の JS チャレンジ問題",
        subtitle="yt-dlp に deno を食わせて GitHub からソルバを落とす",
        bullets=[
            "YouTube は player の難読化JSを定期更新 → 古い yt-dlp は decipher 失敗",
            "新しい yt-dlp は外部 JS ランタイム + リモートコンポーネントで吸収",
            "Dockerfile で deno を入れて、ytdl_format_options に渡すだけで通る",
            "学び: 『公式追従』を CI/CD ではなく 依存ランタイムに任せる発想",
        ],
        code=(
            "ytdl_format_options = {\n"
            "    'format': 'bestaudio/best',\n"
            "    'postprocessors': [{\n"
            "        'key': 'FFmpegExtractAudio',\n"
            "        'preferredcodec': 'opus',\n"
            "        'preferredquality': '256',\n"
            "    }],\n"
            "    # 新しい yt-dlp の機能でJSチャレンジを外部解決\n"
            "    'js_runtimes': {'deno': {}},\n"
            "    'remote_components': ['ejs:github'],\n"
            "}\n"
            "\n"
            "# Dockerfile に deno を入れておく:\n"
            "#   RUN curl -fsSL https://deno.land/install.sh | sh\n"
        ),
        note="ここはクスッと笑いどころ。『JS実行のために deno を抱える Pythonアプリ』。",
    ),
    Slide(
        layout="content",
        title="工夫⑤ async と sync の橋渡し",
        subtitle="voice_client.play() のコールバックを Future にする",
        bullets=[
            "VoiceClient.play は コールバック型 (after=cb) — await できない",
            "Future を作って after で set_result → そのまま await",
            "yt-dlp は同期APIなので loop.run_in_executor でブロックを逃す",
            "再生キューのループは『await Future → 次の曲を pop』のシンプルな形に収束",
        ],
        code=(
            "def awaitable_voice_client_play(\n"
            "    vc, player, loop\n"
            "):\n"
            "    f = asyncio.Future()\n"
            "    def after(e):\n"
            "        loop.call_soon_threadsafe(\n"
            "            lambda: f.set_result(e))\n"
            "    vc.play(player, after=after,\n"
            "            bitrate=256, signal_type='music')\n"
            "    return f\n"
            "\n"
            "# 呼び出し側\n"
            "await awaitable_voice_client_play(\n"
            "    ctx.guild.voice_client, player, client.loop)\n"
        ),
        note="このイディオムだけ覚えて帰ってもらえれば成功。",
    ),
    Slide(
        layout="content",
        title="ハマったところ TOP3",
        subtitle="ライブで踏むと致命傷ぞろい",
        bullets=[
            "① Interaction は 3秒以内に応答 — 重い処理は defer() してから followup",
            "② seek後の出力フォーマット — Opus用 -f opus / PCM用 -f s16le を間違えると無音",
            "③ ボイスチャンネルに人がいなくなったら自動退出 — on_voice_state_update で処理",
        ],
        code=(
            "@client.event\n"
            "async def on_voice_state_update(\n"
            "    member, before, after\n"
            "):\n"
            "    vch = before.channel\n"
            "    vcl = discord.utils.get(\n"
            "        client.voice_clients, channel=vch)\n"
            "    if vcl is None:\n"
            "        return\n"
            "    bots = sum(1 for u in vch.members if u.bot)\n"
            "    if (len(vch.members) == 1\n"
            "        or bots == len(vch.members)\n"
            "    ) and vcl.is_connected():\n"
            "        await vcl.disconnect()\n"
        ),
        note="この自動切断、思った以上に喜ばれる。",
    ),
    Slide(
        layout="content",
        title="残った宿題",
        subtitle="LT 後にやりたいこと",
        bullets=[
            "SQL を psycopg2 の f-string 連結から $1 プレースホルダへ移行 (SQLi予防)",
            "smile_music.py / 2.py / 3.py の3並列を 1コードベース + プロファイル化",
            "再生位置の自前カウントを ffmpeg の -progress に置き換え",
            "テストが無いので、せめてキューロジックはユニットテスト化",
            "loudnorm 二段パスで音量統一 (今は単発パス)",
        ],
        note="改善ネタは無限にある。発表後に Issue にする予定。",
    ),
    Slide(
        layout="finale",
        title="まとめ",
        subtitle="discord.py を一段降りて、ffmpeg と yt-dlp をパーツとして扱う",
        note="コードは公開中: github.com/OGA45/SmileMusic   ―   ご清聴ありがとうございました / Q&A",
    ),
]


def main():
    out_dir = Path(__file__).resolve().parent
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "LT_SmileMusic.pptx"

    prs = Presentation()
    prs.slide_width = SLIDE_W
    prs.slide_height = SLIDE_H
    blank_layout = prs.slide_layouts[6]

    total = len(SLIDES)
    for idx, s in enumerate(SLIDES, start=1):
        slide = prs.slides.add_slide(blank_layout)
        RENDERERS[s.layout](slide, s)
        if s.layout == "content":
            add_footer(slide, idx, total)

    # PowerPointで開いていてロックされている場合は番号付きにフォールバック
    target = out_path
    n = 2
    while True:
        try:
            prs.save(target)
            break
        except PermissionError:
            target = out_dir / f"LT_SmileMusic_v{n}.pptx"
            n += 1
    print(f"wrote {target}  ({total} slides)")


if __name__ == "__main__":
    main()
