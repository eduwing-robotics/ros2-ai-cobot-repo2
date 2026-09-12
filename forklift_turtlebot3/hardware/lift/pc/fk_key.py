r"""
리프트 조작기 — 누르고 있으면 움직이고 떼면 멈춘다. 원하는 높이 4개를 기억시킬 수 있다.

  python3 fk_key.py [파이IP] [포트]      기본: 192.168.20.100 5005

파이에서 데몬을 먼저 띄울 것:  ~/forklift/jogd_start

조작
  ↑ / W          위로 (누르는 동안만)
  ↓ / S          아래로 (누르는 동안만)
  스페이스        정지
  1 2 3 4        기억해 둔 높이로 이동
  Ctrl+1~4       지금 높이를 그 번호에 기억
  [ / ]          상승 속도 느리게 / 빠르게 (50us)
  - / =          하강 속도 느리게 / 빠르게
  0              지금 위치를 영점으로
  , / .          하한 지정 / 상한 지정
  \              리밋 해제
  Ctrl+S         설정 저장 (전원 꺼도 유지)
  Esc            종료
"""
import json, socket, sys, time, tkinter as tk
from tkinter import font as tkfont

HOST = sys.argv[1] if len(sys.argv) > 1 else '192.168.20.100'
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 5005
REPEAT_MS = 40        # 누르고 있는 동안 명령 재전송 간격
RELEASE_MS = 40       # 오토리핏 걸러내는 유예

BG, FG, DIM = '#15171c', '#e8eaf0', '#8b93a7'
BLUE, GREEN, GREY = '#2b6cb0', '#2f855a', '#252a34'

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.settimeout(0.3)
_seq = [0]


def send(cmd):
    """순번을 붙여 보내고 그 순번의 응답만 받는다 (UDP 응답 밀림 방지)"""
    _seq[0] += 1
    tag = str(_seq[0])
    try:
        sock.sendto(f'{tag}|{cmd}'.encode(), (HOST, PORT))
    except Exception as e:
        return {'err': str(e)}
    t0 = time.time()
    while time.time() - t0 < 0.35:
        try:
            st = json.loads(sock.recvfrom(4096)[0].decode())
        except Exception:
            break
        if st.get('seq') == tag:
            return st
    return {'err': '응답 없음'}


class App:
    def __init__(self, root):
        self.root = root
        self.held = None
        self.release_job = None
        self.repeat_job = None
        self.last = {}

        root.title(f'리프트 조작 — {HOST}:{PORT}')
        root.configure(bg=BG)
        root.geometry('640x620')

        big = tkfont.Font(size=34, weight='bold')
        mid = tkfont.Font(size=13)
        sml = tkfont.Font(size=10)
        tiny = tkfont.Font(size=9)

        self.v_mm = tk.Label(root, text='—', font=big, fg=FG, bg=BG)
        self.v_mm.pack(pady=(14, 0))
        self.v_pos = tk.Label(root, text='', font=mid, fg=DIM, bg=BG)
        self.v_pos.pack()
        self.v_state = tk.Label(root, text='정지', font=mid, fg=DIM, bg=BG)
        self.v_state.pack(pady=(6, 4))

        # ── 누르고 있으면 움직이는 버튼 ──
        bar = tk.Frame(root, bg=BG); bar.pack(pady=4)
        self.b_up = tk.Button(bar, text='▲  올림', width=11, height=2, font=mid,
                              bg=BLUE, fg='white', relief='flat', activebackground='#4a90d9')
        self.b_st = tk.Button(bar, text='■ 정지', width=8, height=2, font=mid,
                              bg='#7b341e', fg='white', relief='flat',
                              command=self.stop_now)
        self.b_dn = tk.Button(bar, text='▼  내림', width=11, height=2, font=mid,
                              bg=BLUE, fg='white', relief='flat', activebackground='#4a90d9')
        self.b_up.grid(row=0, column=0, padx=5)
        self.b_st.grid(row=0, column=1, padx=5)
        self.b_dn.grid(row=0, column=2, padx=5)
        self.b_up.bind('<ButtonPress-1>',   lambda e: self.start('U'))
        self.b_up.bind('<ButtonRelease-1>', lambda e: self.stop_now())
        self.b_dn.bind('<ButtonPress-1>',   lambda e: self.start('D'))
        self.b_dn.bind('<ButtonRelease-1>', lambda e: self.stop_now())

        # ── 기억 높이 4칸 ──
        tk.Label(root, text='기억한 높이  —  숫자키로 이동,  Ctrl+숫자 로 지금 높이를 저장',
                 font=sml, fg=DIM, bg=BG).pack(pady=(18, 5))
        mf = tk.Frame(root, bg=BG); mf.pack()
        self.mbtn, self.msave = [], []
        for i in range(1, 5):
            col = tk.Frame(mf, bg=BG)
            col.grid(row=0, column=i - 1, padx=6)
            b = tk.Button(col, text=f'{i}\n비어 있음', width=11, height=3, font=sml,
                          relief='flat', bg=GREY, fg=DIM,
                          command=lambda n=i: self.go_mem(n))
            b.pack()
            sv = tk.Button(col, text='여기 저장', width=11, font=tiny, relief='flat',
                           bg='#31363f', fg=DIM,
                           command=lambda n=i: self.save_mem(n))
            sv.pack(pady=(3, 0))
            self.mbtn.append(b); self.msave.append(sv)

        # ── 설정 표시 ──
        self.v_cfg = tk.Label(root, text='', font=sml, fg='#6b7280', bg=BG, justify='center')
        self.v_cfg.pack(pady=(16, 0))

        tk.Label(root, text=('↑/W 올림   ↓/S 내림   스페이스 정지   1~4 이동   Ctrl+1~4 저장\n'
                             '[ ] 상승속도   - = 하강속도   0 영점   , 하한   . 상한   '
                             '\\ 리밋해제   Ctrl+S 저장   Esc 종료'),
                 font=sml, fg='#5a6273', bg=BG, justify='center').pack(pady=(12, 0))

        self.v_note = tk.Label(root, text='', font=sml, fg='#d69e2e', bg=BG)
        self.v_note.pack(pady=(8, 0))

        root.bind('<KeyPress>', self.on_key)
        root.bind('<KeyRelease>', self.on_key)
        root.protocol('WM_DELETE_WINDOW', self.quit)
        root.focus_force()
        self.poll()

    # ── 키 ──
    def on_key(self, ev):
        k = ev.keysym.lower()
        ctrl = bool(ev.state & 0x4)
        d = None if ctrl else {'up': 'U', 'w': 'U', 'down': 'D', 's': 'D'}.get(k)

        if ev.type == tk.EventType.KeyPress:
            if d:
                if self.release_job:              # 오토리핏 — 뗀 게 아님
                    self.root.after_cancel(self.release_job)
                    self.release_job = None
                self.start(d)
            else:
                self.hotkey(k, ctrl)
        elif ev.type == tk.EventType.KeyRelease and d and self.held == d:
            if self.release_job:
                self.root.after_cancel(self.release_job)
            self.release_job = self.root.after(RELEASE_MS, self.stop_now)

    def hotkey(self, k, ctrl):
        st = self.last
        if k == 'escape':
            self.quit()
        elif k == 'space':
            self.stop_now()
        elif k in '1234' and len(k) == 1:
            (self.save_mem if ctrl else self.go_mem)(int(k))
        elif k == 'bracketleft':
            self.res(send(f"VU{st.get('up_us', 1400) + 50}"))
        elif k == 'bracketright':
            self.res(send(f"VU{max(250, st.get('up_us', 1400) - 50)}"))
        elif k == 'minus':
            self.res(send(f"VD{st.get('down_us', 1400) + 50}"))
        elif k == 'equal':
            self.res(send(f"VD{max(250, st.get('down_us', 1400) - 50)}"))
        elif k == '0':
            self.res(send('Z'))
        elif k == 'comma':
            self.res(send('LH'))
        elif k == 'period':
            self.res(send('LT'))
        elif k == 'backslash':
            self.res(send('L'))
        elif k == 's' and ctrl:
            self.res(send('W'))

    # ── 기억 높이 ──
    def go_mem(self, n):
        self.res(send(f'J{n}'))

    def save_mem(self, n):
        self.res(send(f'E{n}'))
        self.res(send('W'))            # 기억은 바로 저장해 둔다

    # ── 이동 ──
    def start(self, d):
        if self.held == d:
            return
        self.held = d
        self.tick()

    def tick(self):
        if not self.held:
            return
        self.res(send(self.held))
        self.repeat_job = self.root.after(REPEAT_MS, self.tick)

    def stop_now(self):
        self.release_job = None
        self.held = None
        if self.repeat_job:
            self.root.after_cancel(self.repeat_job)
            self.repeat_job = None
        for _ in range(2):             # 정지는 두 번 (한 발 흘려도 서게)
            self.res(send('S'))

    # ── 표시 ──
    def res(self, st):
        if not st or 'err' in st:
            self.v_state.config(text=f"연결 안 됨 — {st.get('err','?') if st else '?'}",
                                fg='#e05252')
            return st
        self.last = st
        self.v_mm.config(text=f"{st['mm']:.1f} mm")
        self.v_pos.config(text=f"위치 {st['pos']} 스텝")
        d = st['dir']
        self.v_state.config(text={1: '▲ 올라가는 중', -1: '▼ 내려가는 중'}.get(d, '정지'),
                            fg={1: '#4ade80', -1: '#60a5fa'}.get(d, DIM))

        for i, mm in enumerate(st.get('mem_mm') or [None] * 4):
            if mm is None:
                self.mbtn[i].config(text=f'{i+1}\n비어 있음', bg=GREY, fg=DIM)
            else:
                near = abs(mm - st['mm']) < 0.3
                self.mbtn[i].config(text=f'{i+1}\n{mm:.1f} mm',
                                    bg=GREEN if near else BLUE, fg='white')

        lim = (f"{st['lo']}~{st['hi']}  ({st['lo']/4096*st['lead']:.0f}"
               f"~{st['hi']/4096*st['lead']:.0f}mm)") if st['limit_on'] else '해제됨'
        self.v_cfg.config(text=(f"상승 {st.get('up_us',0)}us   하강 {st.get('down_us',0)}us   "
                                f"{'풀스텝' if st['full'] else '하프스텝'}\n"
                                f"리드 {st['lead']}mm/회전   리밋 {lim}"))
        if st.get('note'):
            self.v_note.config(text=st['note'])
        return st

    def poll(self):
        if not self.held:
            self.res(send('?'))
        self.root.after(250, self.poll)

    def quit(self):
        try:
            send('S')
        except Exception:
            pass
        self.root.destroy()


if __name__ == '__main__':
    print(__doc__)
    r = tk.Tk()
    App(r)
    r.mainloop()
