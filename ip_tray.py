#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IP Tray — внешний IP с 2ip.ru и Яндекс.Интернетометра.
Живёт в системном трее (область уведомлений панели задач).
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import webbrowser
import socket
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import requests
from PIL import Image, ImageDraw, ImageFont

try:
    import pystray
    from pystray import MenuItem as Item
except ImportError:
    print("Установите зависимости: pip install -r requirements.txt")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Константы
# ---------------------------------------------------------------------------

APP_NAME = "IP Tray"
REFRESH_SECONDS = 60
REQUEST_TIMEOUT = 8
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)

URL_2IP = "https://2ip.ru/"
URL_YANDEX = "https://yandex.ru/internet/"
YANDEX_IP_ENDPOINTS = [
    "https://yandex.ru/internet/api/v0/ip",
    "https://ya.ru/internet/api/v0/ip",
    "https://ipv4.internet.yandex.net/internet/api/v0/ip",
]
TWOIP_JSON_ENDPOINTS = [
    "https://api.2ip.me/provider.json",
    "https://api.2ip.ua/provider.json",
    "http://api.2ip.me/provider.json",
]
# 2ip.ru часто отвечает заглушкой 503; зеркала 2ip.ua / 2ip.me той же сети отдают IP в HTML.
TWOIP_PAGE_ENDPOINTS = [
    "https://2ip.ua/ru/",
    "https://2ip.ua/",
    "https://2ip.me/ru/",
    "https://2ip.me/",
    "https://2ip.ru/",
    "https://www.2ip.ru/",
]

IPV4_RE = re.compile(
    r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}"
    r"(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b"
)

if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(APP_DIR, "ip_tray_state.json")


# ---------------------------------------------------------------------------
# Данные
# ---------------------------------------------------------------------------

@dataclass
class IpSnapshot:
    twoip: str = "—"
    twoip_extra: str = ""
    yandex: str = "—"
    yandex_extra: str = ""
    local_ip: str = "—"
    updated_at: Optional[datetime] = None
    error: str = ""
    history: list = field(default_factory=list)

    def same_public(self) -> bool:
        if self.twoip in ("—", "ошибка") or self.yandex in ("—", "ошибка"):
            return False
        return self.twoip == self.yandex

    def tooltip(self) -> str:
        # Windows NOTIFYICONDATAW.szTip — максимум 128 символов.
        ts = self.updated_at.strftime("%H:%M") if self.updated_at else "--:--"
        lines = [
            f"2ip {self.twoip}",
            f"Янд {self.yandex}",
            f"лок {self.local_ip}  {ts}",
        ]
        if (
            not self.same_public()
            and self.twoip not in ("—", "ошибка")
            and self.yandex not in ("—", "ошибка")
        ):
            lines.append("разные IP")
        text = "\n".join(lines)
        return text[:127]


# ---------------------------------------------------------------------------
# Сеть
# ---------------------------------------------------------------------------

def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/json,text/plain,*/*",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            "Cache-Control": "no-cache",
        }
    )
    return s


def get_local_ip() -> str:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(1)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "—"


def fetch_yandex_ip(sess: requests.Session) -> tuple[str, str]:
    last_err = ""
    for url in YANDEX_IP_ENDPOINTS:
        try:
            r = sess.get(url, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            text = r.text.strip()
            try:
                data = r.json()
                if isinstance(data, str):
                    text = data.strip().strip('"')
                elif isinstance(data, dict):
                    text = str(data.get("ip") or data.get("ipv4") or text)
            except ValueError:
                text = text.strip().strip('"')
            m = IPV4_RE.search(text)
            if m:
                return m.group(0), ""
            last_err = f"неожиданный ответ: {text[:80]}"
        except Exception as exc:
            last_err = str(exc)
    return "ошибка", last_err


def _extract_2ip_meta(html: str, ip: str) -> str:
    """Достаём провайдера/город рядом с IP, если страница отдала HTML."""
    extra_bits = []
    # типичные подписи на 2ip.ru
    patterns = [
        r"(?:Провайдер|ISP|Оператор)[^:<]{0,40}:\s*</?\w*[^>]*>\s*([^<]{2,80})",
        r"(?:Город|City)[^:<]{0,40}:\s*</?\w*[^>]*>\s*([^<]{2,80})",
        r'class="[^"]*(?:provider|isp|city)[^"]*"[^>]*>\s*([^<]{2,80})',
    ]
    for pat in patterns:
        m = re.search(pat, html, flags=re.IGNORECASE)
        if m:
            val = re.sub(r"\s+", " ", m.group(1)).strip(" \n\r\t·|-")
            if val and val not in extra_bits and ip not in val:
                extra_bits.append(val)
        if len(extra_bits) >= 2:
            break
    return " · ".join(extra_bits[:2])


def _is_public_ipv4(ip: str) -> bool:
    try:
        a, b, *_ = map(int, ip.split("."))
    except ValueError:
        return False
    if a in (0, 10, 127) or a >= 224:
        return False
    if a == 192 and b == 168:
        return False
    if a == 172 and 16 <= b <= 31:
        return False
    if a == 169 and b == 254:
        return False
    if ip.startswith("255."):
        return False
    return True


def _ip_from_2ip_html(html: str) -> Optional[str]:
    if "2ip loading" in html or "main-loader" in html:
        return None
    patterns = [
        r'class=["\']ip["\'][^>]*>\s*(' + IPV4_RE.pattern + r")",
        r"(?:Ваш IP(?:-?адрес)?|Your IP address)\s*[:\s<]*[^0-9]{0,80}("
        + IPV4_RE.pattern
        + r")",
        r'(?:id|class)=["\'][^"\']*(?:ip|d_clip)[^"\']*["\'][^>]*>\s*'
        r"(?:<[^>]+>\s*)*?(" + IPV4_RE.pattern + r")",
    ]
    for pat in patterns:
        m = re.search(pat, html, flags=re.IGNORECASE)
        if m and _is_public_ipv4(m.group(1)):
            return m.group(1)
    for m in IPV4_RE.finditer(html):
        cand = m.group(0)
        if _is_public_ipv4(cand):
            return cand
    return None


def fetch_2ip(sess: requests.Session) -> tuple[str, str, str]:
    last_err = ""

    for url in TWOIP_PAGE_ENDPOINTS:
        try:
            r = sess.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
            html = r.text or ""
            ip = _ip_from_2ip_html(html)
            if ip:
                extra = _extract_2ip_meta(html, ip)
                return ip, extra, ""
            if r.status_code >= 400:
                last_err = f"{r.status_code} {url.split('/')[2]}"
            else:
                last_err = f"нет IP на {url.split('/')[2]}"
        except Exception as exc:
            last_err = str(exc)[:80]

    for url in TWOIP_JSON_ENDPOINTS:
        try:
            r = sess.get(url, timeout=REQUEST_TIMEOUT)
            if r.status_code == 429:
                last_err = "лимит API 2ip"
                continue
            r.raise_for_status()
            data = r.json()
            ip = str(data.get("ip") or "")
            if not IPV4_RE.fullmatch(ip):
                continue
            extra_parts = [
                data.get("name_rus") or data.get("name_ripe") or data.get("name_ua") or "",
                data.get("as") and f"AS{data.get('as')}",
            ]
            extra = " · ".join(p for p in extra_parts if p)
            return ip, extra, ""
        except Exception as exc:
            last_err = str(exc)[:80]

    return "ошибка", "", last_err or "2ip недоступен"


def refresh_snapshot(prev: Optional[IpSnapshot] = None) -> IpSnapshot:
    snap = IpSnapshot()
    snap.local_ip = get_local_ip()
    errors = []
    sess = _session()

    def load_yandex():
        ip, err = fetch_yandex_ip(sess)
        snap.yandex = ip
        if err:
            errors.append("Яндекс: " + err)

    def load_2ip():
        ip, extra, err = fetch_2ip(sess)
        snap.twoip = ip
        snap.twoip_extra = extra
        if err:
            errors.append("2ip: " + err)

    t1 = threading.Thread(target=load_yandex, daemon=True)
    t2 = threading.Thread(target=load_2ip, daemon=True)
    t1.start()
    t2.start()
    t1.join(REQUEST_TIMEOUT + 2)
    t2.join(REQUEST_TIMEOUT + 2)

    snap.updated_at = datetime.now()
    snap.error = "; ".join(errors)[:180]
    if prev and prev.history:
        snap.history = list(prev.history)
    public = snap.twoip if snap.twoip not in ("—", "ошибка") else snap.yandex
    if public not in ("—", "ошибка"):
        last = snap.history[-1]["ip"] if snap.history else None
        if last != public:
            snap.history.append(
                {"ip": public, "at": snap.updated_at.strftime("%Y-%m-%d %H:%M:%S")}
            )
            snap.history = snap.history[-20:]
    return snap


# ---------------------------------------------------------------------------
# Иконка
# ---------------------------------------------------------------------------

def _font(size: int) -> ImageFont.ImageFont:
    for name in (
        "segoeui.ttf",
        "arial.ttf",
        "DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def make_icon(snap: IpSnapshot, size: int = 64) -> Image.Image:
    """Квадратная иконка: последние октеты IP или статус."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    ip = snap.twoip if snap.twoip not in ("—", "ошибка") else snap.yandex
    if ip in ("—", "ошибка") or not snap.updated_at:
        bg = (70, 70, 78, 255)
        label = "IP"
        sub = ""
    elif not snap.same_public() and snap.twoip not in ("—", "ошибка") and snap.yandex not in ("—", "ошибка"):
        bg = (180, 110, 20, 255)
        parts = ip.split(".")
        label = parts[-1]
        sub = parts[-2]
    else:
        bg = (20, 120, 80, 255)
        parts = ip.split(".")
        label = parts[-1]
        sub = parts[-2]

    r = size // 6
    draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=r, fill=bg)

    if sub:
        f_main = _font(max(18, size // 2 - 4))
        f_sub = _font(max(11, size // 4))
        # два октета: xx / yy
        bbox_s = draw.textbbox((0, 0), sub, font=f_sub)
        bbox_m = draw.textbbox((0, 0), label, font=f_main)
        draw.text(
            ((size - (bbox_s[2] - bbox_s[0])) / 2, size * 0.06),
            sub,
            font=f_sub,
            fill=(255, 255, 255, 210),
        )
        draw.text(
            ((size - (bbox_m[2] - bbox_m[0])) / 2, size * 0.34),
            label,
            font=f_main,
            fill=(255, 255, 255, 255),
        )
    else:
        f_main = _font(max(16, size // 3))
        bbox = draw.textbbox((0, 0), label, font=f_main)
        draw.text(
            ((size - (bbox[2] - bbox[0])) / 2, (size - (bbox[3] - bbox[1])) / 2 - 2),
            label,
            font=f_main,
            fill=(255, 255, 255, 255),
        )
    return img


# ---------------------------------------------------------------------------
# Буфер обмена / автозапуск
# ---------------------------------------------------------------------------

def copy_text(text: str) -> None:
    if text in ("—", "ошибка", ""):
        return
    try:
        import tkinter as tk

        r = tk.Tk()
        r.withdraw()
        r.clipboard_clear()
        r.clipboard_append(text)
        r.update()
        r.destroy()
        return
    except Exception:
        pass
    if sys.platform == "win32":
        try:
            import subprocess

            subprocess.run("clip", input=text.encode("utf-16le"), check=False)
        except Exception:
            pass


def startup_path() -> Optional[str]:
    if sys.platform != "win32":
        return None
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    folder = os.path.join(
        appdata, r"Microsoft\Windows\Start Menu\Programs\Startup"
    )
    return os.path.join(folder, "IP Tray.bat")


def is_autostart_enabled() -> bool:
    path = startup_path()
    return bool(path and os.path.isfile(path))


def set_autostart(enabled: bool) -> None:
    path = startup_path()
    if not path:
        return
    if enabled:
        if getattr(sys, "frozen", False):
            target = os.path.abspath(sys.executable)
            cmd = f'@echo off\nstart "" "{target}"\n'
        else:
            target = os.path.abspath(sys.argv[0])
            cmd = f'@echo off\nstart "" "{sys.executable}" "{target}"\n'
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(cmd)
    elif os.path.isfile(path):
        os.remove(path)


# ---------------------------------------------------------------------------
# Окно подробностей (рядом с панелью задач)
# ---------------------------------------------------------------------------

class DetailWindow:
    def __init__(self, app: "TrayApp"):
        self.app = app
        self.root = None

    def is_open(self) -> bool:
        return self.root is not None

    def show(self) -> None:
        if self.root is not None:
            try:
                self.root.deiconify()
                self.root.lift()
                self._render()
                return
            except Exception:
                self.root = None
        self._build()

    def hide(self) -> None:
        if self.root is not None:
            try:
                self.root.destroy()
            except Exception:
                pass
            self.root = None

    def toggle(self) -> None:
        if self.is_open():
            self.hide()
        else:
            self.show()

    def _build(self) -> None:
        import tkinter as tk
        from tkinter import font as tkfont

        root = tk.Tk()
        self.root = root
        root.title(APP_NAME)
        root.resizable(False, False)
        root.attributes("-topmost", True)
        try:
            root.overrideredirect(False)
        except Exception:
            pass

        bg = "#1c1c1f"
        fg = "#f2f2f2"
        muted = "#9a9aa3"
        accent = "#3dcc8a"
        root.configure(bg=bg)

        self.vars = {
            "twoip": tk.StringVar(),
            "yandex": tk.StringVar(),
            "local": tk.StringVar(),
            "time": tk.StringVar(),
            "note": tk.StringVar(),
        }

        pad = {"padx": 16, "pady": 2}
        title = tk.Label(
            root, text="Внешний IP", bg=bg, fg=fg,
            font=tkfont.Font(size=12, weight="bold"),
        )
        title.grid(row=0, column=0, columnspan=3, sticky="w", padx=16, pady=(12, 8))

        def row(r, label, key, copy_value):
            tk.Label(root, text=label, bg=bg, fg=muted, font=("Segoe UI", 9)).grid(
                row=r, column=0, sticky="w", **pad
            )
            tk.Label(
                root, textvariable=self.vars[key], bg=bg, fg=fg,
                font=("Consolas", 12, "bold"),
            ).grid(row=r, column=1, sticky="w", **pad)
            tk.Button(
                root, text="копировать", command=lambda: copy_text(copy_value()),
                relief="flat", bg="#2a2a30", fg=fg, activebackground="#3a3a42",
                cursor="hand2", padx=8,
            ).grid(row=r, column=2, padx=12, pady=2)

        row(1, "2ip.ru", "twoip", lambda: self.app.snap.twoip)
        row(2, "Яндекс", "yandex", lambda: self.app.snap.yandex)
        row(3, "Локальный", "local", lambda: self.app.snap.local_ip)

        tk.Label(root, textvariable=self.vars["time"], bg=bg, fg=muted, font=("Segoe UI", 8)).grid(
            row=4, column=0, columnspan=3, sticky="w", padx=16, pady=(6, 0)
        )
        tk.Label(root, textvariable=self.vars["note"], bg=bg, fg="#e0b44a", font=("Segoe UI", 8), wraplength=320, justify="left").grid(
            row=5, column=0, columnspan=3, sticky="w", padx=16, pady=(0, 4)
        )

        btns = tk.Frame(root, bg=bg)
        btns.grid(row=6, column=0, columnspan=3, pady=(8, 14), padx=16, sticky="ew")

        def btn(text, cmd):
            return tk.Button(
                btns, text=text, command=cmd, relief="flat",
                bg="#2a2a30", fg=fg, activebackground="#3a3a42",
                cursor="hand2", padx=10, pady=4,
            )

        btn("Обновить", self.app.refresh_now).pack(side="left", padx=(0, 6))
        btn("2ip.ru", lambda: webbrowser.open(URL_2IP)).pack(side="left", padx=6)
        btn("Интернетометр", lambda: webbrowser.open(URL_YANDEX)).pack(side="left", padx=6)
        btn("Скрыть", self.hide).pack(side="right")

        root.protocol("WM_DELETE_WINDOW", self.hide)
        self._place_near_tray(root)
        self._render()

        def pump():
            if self.root is None:
                return
            try:
                self.root.update()
                self.root.after(200, pump)
            except Exception:
                self.root = None

        root.after(200, pump)

    def _place_near_tray(self, root) -> None:
        root.update_idletasks()
        w, h = 380, 230
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        # правый нижний угол — над панелью задач
        x = sw - w - 16
        y = sh - h - 72
        root.geometry(f"{w}x{h}+{x}+{y}")

    def _render(self) -> None:
        if self.root is None:
            return
        s = self.app.snap
        self.vars["twoip"].set(s.twoip + (f"  {s.twoip_extra}" if s.twoip_extra else ""))
        self.vars["yandex"].set(s.yandex)
        self.vars["local"].set(s.local_ip)
        ts = s.updated_at.strftime("%H:%M:%S") if s.updated_at else "ещё не обновлялось"
        self.vars["time"].set(f"Обновлено: {ts}   интервал {REFRESH_SECONDS} с")
        note = ""
        if s.twoip not in ("—", "ошибка") and s.yandex not in ("—", "ошибка") and s.twoip != s.yandex:
            note = "Сервисы вернули разные адреса — проверьте VPN/прокси."
        elif s.error:
            note = s.error
        self.vars["note"].set(note)


# ---------------------------------------------------------------------------
# Приложение
# ---------------------------------------------------------------------------

class TrayApp:
    def __init__(self):
        self.snap = IpSnapshot()
        self._stop = threading.Event()
        self.window = DetailWindow(self)
        self.icon: Optional[pystray.Icon] = None

    def run(self) -> None:
        self.icon = pystray.Icon(
            APP_NAME,
            make_icon(self.snap),
            APP_NAME,
            menu=self._menu(),
        )
        # первый запрос после появления иконки
        self.icon.run(setup=self._setup)

    def _setup(self, icon: pystray.Icon) -> None:
        icon.visible = True
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        first = True
        while not self._stop.is_set():
            old_ip = self.snap.twoip
            try:
                self.snap = refresh_snapshot(self.snap)
                self._apply_ui()
                if (
                    not first
                    and old_ip not in ("—", "ошибка", "")
                    and self.snap.twoip not in ("—", "ошибка")
                    and self.snap.twoip != old_ip
                ):
                    self._notify(f"IP: {old_ip} → {self.snap.twoip}")
            except Exception as exc:
                self.snap.error = str(exc)[:80]
                try:
                    self._apply_ui()
                except Exception:
                    pass
            first = False
            self._stop.wait(REFRESH_SECONDS)

    def _apply_ui(self) -> None:
        if not self.icon:
            return
        try:
            self.icon.icon = make_icon(self.snap)
        except Exception:
            pass
        try:
            self.icon.title = self.snap.tooltip()[:127]
        except Exception:
            try:
                self.icon.title = "IP Tray"
            except Exception:
                pass
        try:
            self.icon.menu = self._menu()
        except Exception:
            pass
        if self.window.is_open():
            try:
                self.window._render()
            except Exception:
                pass

    def _menu(self):
        s = self.snap
        same = "совпадают" if s.same_public() else "различаются"

        def checked_autostart(item):
            return is_autostart_enabled()

        return pystray.Menu(
            Item(lambda item: f"2ip.ru:  {s.twoip}", lambda *_: copy_text(s.twoip)),
            Item(lambda item: f"Яндекс:  {s.yandex}", lambda *_: copy_text(s.yandex)),
            Item(lambda item: f"Локальный:  {s.local_ip}", lambda *_: copy_text(s.local_ip)),
            Item(lambda item: f"Статус IP: {same}", None, enabled=False),
            pystray.Menu.SEPARATOR,
            Item("Обновить сейчас", self.refresh_now),
            Item("Показать окно", lambda *_: self.window.show()),
            Item("Копировать IP 2ip", lambda *_: copy_text(s.twoip)),
            Item("Копировать IP Яндекса", lambda *_: copy_text(s.yandex)),
            pystray.Menu.SEPARATOR,
            Item("Открыть 2ip.ru", lambda *_: webbrowser.open(URL_2IP)),
            Item("Открыть Интернетометр", lambda *_: webbrowser.open(URL_YANDEX)),
            pystray.Menu.SEPARATOR,
            Item(
                "Автозапуск с Windows",
                self._toggle_autostart,
                checked=checked_autostart,
            ),
            Item("Выход", self.quit),
        )

    def refresh_now(self, *_) -> None:
        def work():
            self.snap = refresh_snapshot(self.snap)
            self._apply_ui()

        threading.Thread(target=work, daemon=True).start()

    def _toggle_autostart(self, *_) -> None:
        set_autostart(not is_autostart_enabled())
        if self.icon:
            self.icon.menu = self._menu()

    def _notify(self, message: str) -> None:
        try:
            if self.icon:
                self.icon.notify(message, APP_NAME)
        except Exception:
            pass

    def quit(self, *_) -> None:
        self._stop.set()
        self.window.hide()
        if self.icon:
            self.icon.stop()


def main() -> None:
    # скрыть консоль на Windows, если запустили pythonw / .pyw
    TrayApp().run()


if __name__ == "__main__":
    main()
