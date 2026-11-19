"""Small native preparation-name editor and per-player region calibration."""
from __future__ import annotations

import math
import copy
import tkinter as tk
from tkinter import ttk, filedialog
from pathlib import Path

from .dialog_style import FONT, configure_dialog
from .settings import ROOT, atomic_json, f_path
from .theme import ACCENT, BG


class PrepDialog:
    def __init__(self, owner):
        self.owner = owner
        self.root = tk.Toplevel(owner.root)
        self.root.title('准备阶段 · 名单识别与校正')
        self.root.geometry(f'940x{min(720, self.root.winfo_screenheight() - 80)}')
        configure_dialog(self.root)
        self.root.attributes('-topmost', True)
        self.note = tk.StringVar(value='已附带常见地图投票界面位置；布局不同时请重新框选。已识别名字可在这里校正；同名候选需要你明确选择。')
        ttk.Label(self.root, textvariable=self.note, style='Muted.BA.TLabel', wraplength=900,
                  justify='left', anchor='w').pack(fill='x', padx=16, pady=(14, 5))
        tabs = ttk.Notebook(self.root, style='BA.TNotebook')
        tabs.pack(fill='both', expand=True, padx=14, pady=8)
        names = ttk.Frame(tabs, style='BA.TFrame', padding=(12, 6))
        regions = ttk.Frame(tabs, style='BA.TFrame', padding=(8, 5))
        tabs.add(names, text='名单校正 / 手动查询')
        tabs.add(regions, text='设置自动识别区域')
        self.entries = {}
        self.selectors = {}
        self.candidates = {}
        ttk.Label(names, style='BA.TLabel', text='我方 / 对方由你框选的区域决定。留空的人显示缺失；搜索同名时请选对数字 ID。',
                  wraplength=850, justify='left').grid(row=0, column=0, columnspan=3, sticky='w', padx=4, pady=(6, 10))
        for team_index, team in enumerate(('Bravo', 'Alpha')):
            for index in range(5):
                row = 1 + team_index * 5 + index
                gap = (11, 2) if team == 'Alpha' and index == 0 else (2, 2)
                ttk.Label(names, style='BA.TLabel', text=f"{'对手' if team == 'Bravo' else '我方'} {index + 1}").grid(
                    row=row, column=0, sticky='w', padx=(4, 12), pady=gap)
                value = tk.StringVar()
                ttk.Entry(names, textvariable=value, width=29, font=FONT, style='BA.TEntry').grid(
                    row=row, column=1, padx=(0, 8), pady=gap, sticky='ew')
                choice = ttk.Combobox(names, state='readonly', width=45, font=FONT, style='BA.TCombobox')
                choice.grid(row=row, column=2, padx=(0, 4), pady=gap, sticky='ew')
                self.entries[team, index] = value
                self.selectors[team, index] = choice
                self.candidates[team, index] = {}
                value.trace_add('write', lambda *_, selected=choice: selected.set(''))
        names.columnconfigure(1, weight=1)
        names.columnconfigure(2, weight=1)
        ttk.Button(names, style='Primary.BA.TButton', text='按以上名单查询', command=self.query_names).grid(
            row=11, column=0, columnspan=3, sticky='e', padx=4, pady=(12, 6))
        ttk.Label(names, style='Muted.BA.TLabel', text='这里查询的是已结束对局的历史记录。姓名匹配后会标注来源，正式游戏 ID 名单出现后再核对。',
                  wraplength=850, justify='left').grid(row=12, column=0, columnspan=3, sticky='w', padx=4, pady=6)
        bar = ttk.Frame(regions, style='BA.TFrame')
        bar.pack(fill='x', pady=6)
        ttk.Button(bar, style='BA.TButton', text='读取当前游戏画面', command=self.capture).pack(side='left', padx=4)
        ttk.Button(bar, style='BA.TButton', text='载入已有截图', command=self.load_image).pack(side='left', padx=4)
        ttk.Button(bar, style='BA.TButton', text='依次框 5 个对手名字', command=lambda: self.begin('Bravo')).pack(side='left', padx=4)
        ttk.Button(bar, style='BA.TButton', text='依次框 5 个我方名字', command=lambda: self.begin('Alpha')).pack(side='left', padx=4)
        ttk.Label(regions, style='BA.TLabel', text='在缩略图上拖出姓名框，只框完整名字；不含头像、段位、倒计时。每侧依次框5个，顺序不限。',
                 wraplength=900, justify='left').pack(anchor='w', pady=5)
        ttk.Label(regions, style='Muted.BA.TLabel', text='先切回游戏再从悬浮窗读取，移开其他软件窗口。载入的图片须只有完整游戏画面，不带桌面、标题栏或黑边。',
                 wraplength=900, justify='left').pack(anchor='w', pady=(0, 5))
        self.canvas = tk.Canvas(regions, width=890, height=440, background=BG, highlightthickness=0)
        self.canvas.pack(fill='both', expand=True)
        self.canvas.bind('<ButtonPress-1>', self.press)
        self.canvas.bind('<B1-Motion>', self.drag)
        self.canvas.bind('<ButtonRelease-1>', self.release)
        self.regions = copy.deepcopy(owner.config.get('prep_regions') or {})
        self.original = self.preview = None
        self.image_path = None
        self.factor = 1
        self.select_team = None
        self.anchor = None
        self.drag_rect = None
        ttk.Button(regions, style='Primary.BA.TButton', text='保存识别区域并启用自动读取',
                   command=self.save_regions).pack(side='bottom', anchor='e', before=self.canvas, pady=6)
        ttk.Button(self.root, style='BA.TButton', text='关闭', command=self.root.destroy).pack(anchor='e', padx=14, pady=(0, 10))
        self.update_rows(getattr(owner, 'prep_rows', []))
        if not self.regions:
            tabs.select(regions)

    def exists(self):
        return bool(self.root.winfo_exists())

    def update_rows(self, rows):
        # A partial/new generation must not retain unmentioned names or ID
        # choices from the previous match in the editable form.
        for key, entry in self.entries.items():
            entry.set('')
            self.selectors[key].set('')
            self.selectors[key].config(values=[])
            self.candidates[key] = {}
        for row in rows:
            key = (row.get('team'), row.get('index'))
            if key not in self.entries:
                continue
            variants = row.get('variants') or row.get('observed_variants') or []
            first = variants[0] if variants else ''
            if isinstance(first, dict):
                first = first.get('text', '')
            decision = row.get('resolution') or {}
            self.entries[key].set(row.get('name') or decision.get('name') or first)
            candidates = decision.get('candidates') or row.get('candidates') or []
            mapping = {f"{p['name']} · ID {p['id']}": p for p in candidates if p.get('id') and p.get('name')}
            self.candidates[key] = mapping
            self.selectors[key].config(values=list(mapping))
        self.note.set('已更新识别结果。若有同名、错字或缺失，修改后点击“按以上名单查询”。' if rows else
                      '暂无本局姓名。首次使用请先设置识别区域；也可在“名单校正”中输入完整名字立即查询。')

    def query_names(self):
        rows = []
        for (team, index), value in self.entries.items():
            name = value.get().strip()
            if not name:
                continue
            row = dict(team=team, index=index, variants=[name])
            chosen = self.candidates[team, index].get(self.selectors[team, index].get())
            if chosen is not None:
                row.update(player_id=chosen['id'], name=chosen['name'])
            rows.append(row)
        if not rows:
            self.note.set('请先读取或输入至少一个完整玩家名字。')
            return
        self.owner.start_prep_manual(rows)
        self.note.set('正在按以上名单匹配 ID 并查询；结果会逐步显示在悬浮窗。')

    def capture(self):
        try:
            self.owner.ensure_prep_tools()
            future = self.owner.screen_ocr.submit_capture_game(capture_only=True, require_foreground=False)
            self.note.set('正在读取游戏窗口画面，请稍候。')
        except Exception as exc:
            self.note.set('画面读取失败：' + str(exc))
            return

        def poll():
            if not self.exists():
                return
            if not future.done():
                self.root.after(80, poll)
                return
            try:
                result = future.result()
                if not result.get('ok'):
                    hints = {'not_found': '未找到断箭窗口，请先打开游戏。',
                             'minimized': '游戏已最小化，请先切回游戏。',
                             'not_visible': '游戏窗口不可见，请先切回游戏。',
                             'occluded': '其他软件遮住了游戏，请先切回游戏，再从悬浮窗点击读取。',
                             'black_frame': '未取得有效游戏画面，请切回游戏或改用已有的纯游戏截图。',
                             'not_foreground': '请先切回游戏，再从悬浮窗点击读取。'}
                    raise ValueError(hints.get(result.get('status')) or result.get('error') or result.get('status'))
                self.show_image(result['image_path'])
            except Exception as exc:
                self.note.set('画面读取失败：' + str(exc))
        self.root.after(80, poll)

    def load_image(self):
        path = filedialog.askopenfilename(parent=self.root, initialdir=str(ROOT), title='仅选择完整纯游戏画面 PNG（不含桌面或标题栏）',
                                          filetypes=[('PNG 截图', '*.png')])
        if path:
            try:
                self.show_image(path)
            except Exception as exc:
                self.note.set('图片打开失败：' + str(exc))

    def show_image(self, path):
        self.original = tk.PhotoImage(file=str(path), master=self.root)
        self.image_path = str(path)
        self.factor = max(1, math.ceil(self.original.width() / 880), math.ceil(self.original.height() / 440))
        self.preview = self.original.subsample(self.factor)
        self.canvas.delete('all')
        self.canvas.create_image(0, 0, anchor='nw', image=self.preview)
        self.draw_regions()
        self.note.set('画面已读取。点击“依次框5个对手名字”，逐一拖出完整姓名框，再框我方。')

    def begin(self, team):
        if self.preview is None:
            self.note.set('先读取游戏画面或载入准备界面的截图。')
            return
        self.select_team = team
        self.regions[team] = {'rows': []}
        self.draw_regions()
        self.note.set(f"请框出{'对手' if team == 'Bravo' else '我方'}第 1 个完整名字。")

    def press(self, event):
        if self.select_team is None or self.preview is None:
            return
        self.anchor = (max(0, min(event.x, self.preview.width())), max(0, min(event.y, self.preview.height())))
        self.drag_rect = self.canvas.create_rectangle(*self.anchor, *self.anchor, outline=ACCENT, width=2)

    def drag(self, event):
        if self.anchor is not None:
            self.canvas.coords(self.drag_rect, *self.anchor,
                               max(0, min(event.x, self.preview.width())), max(0, min(event.y, self.preview.height())))

    def release(self, event):
        if self.anchor is None:
            return
        self.drag(event)
        x1, y1, x2, y2 = self.canvas.coords(self.drag_rect)
        self.anchor = None
        self.canvas.delete(self.drag_rect)
        if abs(x2-x1) < 8 or abs(y2-y1) < 4:
            self.note.set('框太小，请重新拖出完整名字。')
            return
        x1, x2 = [min(value*self.factor, self.original.width()) for value in (x1, x2)]
        y1, y2 = [min(value*self.factor, self.original.height()) for value in (y1, y2)]
        x, y, width, height = min(x1, x2), min(y1, y2), abs(x2-x1), abs(y2-y1)
        rows = self.regions[self.select_team]['rows']
        rows.append([x/self.original.width(), y/self.original.height(),
                     width/self.original.width(), height/self.original.height()])
        team = self.select_team
        if len(rows) == 5:
            self.select_team = None
            self.note.set(f"{'对手' if team == 'Bravo' else '我方'}5个姓名框已设置。完成另一侧后保存。")
        else:
            self.note.set(f"请框出{'对手' if team == 'Bravo' else '我方'}第 {len(rows)+1} 个完整名字。")
        self.draw_regions()

    def draw_regions(self):
        self.canvas.delete('roi')
        if self.preview is None:
            return
        for team, config in self.regions.items():
            rows = config.get('rows', []) if isinstance(config, dict) else []
            for index, box in enumerate(rows):
                x, y, width, height = box
                x, y = x*self.original.width()/self.factor, y*self.original.height()/self.factor
                width, height = width*self.original.width()/self.factor, height*self.original.height()/self.factor
                color = '#ffb06b' if team == 'Bravo' else '#63dfcd'
                self.canvas.create_rectangle(x, y, x+width, y+height, outline=color, width=2, tags='roi')
                self.canvas.create_text(x, y, anchor='sw', text=f"{'敌' if team == 'Bravo' else '我'}{index+1}", fill=color, tags='roi')

    def save_regions(self):
        try:
            from .prep_roster import extract_prep_roster
            if self.original is None or any(len(self.regions.get(team, {}).get('rows', [])) != 5 for team in ('Alpha', 'Bravo')):
                raise ValueError('请在准备界面截图上为双方各框5个完整名字。')
            # The extractor validates coordinates and conflicting regions before
            # any name is permitted to reach the public search endpoint.
            result = extract_prep_roster({'results': []}, self.regions,
                                         image_size=(self.original.width(), self.original.height()))
            if result.get('geometry_valid') is False:
                raise ValueError('姓名框有重叠或坐标无效，请重新框选。')
            self.owner.config.update(prep_enabled=True, prep_regions=copy.deepcopy(self.regions))
            atomic_json(f_path(ROOT / 'config.json'), self.owner.config)
            self.owner.restart_prep_if_waiting()
            self.note.set('已保存。进入准备房间且游戏在前台时会自动读取；分辨率比例或名单布局改变后请重新框选。')
        except Exception as exc:
            self.note.set('保存失败：' + str(exc))
