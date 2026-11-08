#!/usr/bin/env python3
"""Battleship in your terminal. Play the computer, or a friend over the network."""

import argparse
import getpass
import json
import locale
import os
import queue
import random
import socket
import sys
import threading
import time

try:
    import curses
except ImportError:  # Windows without the windows-curses package
    curses = None

SIZE = 10
FLEET = [("Carrier", 5), ("Battleship", 4), ("Cruiser", 3), ("Submarine", 3), ("Destroyer", 2)]
PROTOCOL_VERSION = 2
DEFAULT_PORT = 9999
ROWS = "ABCDEFGHIJ"
AI_DELAY = 0.7
MAX_LINE = 64 * 1024
MIN_W, MIN_H = 56, 18

UNKNOWN, MISS, HIT, SUNK = range(4)


def label(x, y):
    return f"{ROWS[y]}{x + 1}"


def in_bounds(x, y):
    return 0 <= x < SIZE and 0 <= y < SIZE


def valid_cell(x, y):
    ok = all(isinstance(v, int) and not isinstance(v, bool) for v in (x, y))
    return ok and in_bounds(x, y)


def ship_cells(x, y, length, horizontal):
    return [(x + i, y) if horizontal else (x, y + i) for i in range(length)]


def clamp(pos, length=1, horizontal=True):
    """Keep a ship's starting cell where the whole ship stays on the board."""
    x, y = pos
    max_x = SIZE - (length if horizontal else 1)
    max_y = SIZE - (1 if horizontal else length)
    return min(max(x, 0), max_x), min(max(y, 0), max_y)


class ProtocolError(Exception):
    pass


# --- Rules ------------------------------------------------------------------


class Ship:
    def __init__(self, name, cells):
        self.name = name
        self.cells = list(cells)
        self.hits = set()

    @property
    def sunk(self):
        return len(self.hits) == len(self.cells)


class Board:
    """Your own waters: where your ships are, and where you've been shot."""

    def __init__(self):
        self.ships = []
        self.owner = {}
        self.shots = set()

    @property
    def complete(self):
        return len(self.ships) == len(FLEET)

    def next_ship(self):
        return None if self.complete else FLEET[len(self.ships)]

    def fits(self, cells):
        return all(in_bounds(x, y) and (x, y) not in self.owner for x, y in cells)

    def place(self, name, cells):
        if not self.fits(cells):
            raise ValueError("ship doesn't fit there")
        ship = Ship(name, cells)
        self.ships.append(ship)
        for cell in ship.cells:
            self.owner[cell] = ship

    def undo(self):
        if self.ships:
            for cell in self.ships.pop().cells:
                del self.owner[cell]

    def place_randomly(self, rng):
        self.ships, self.owner = [], {}
        for name, length in FLEET:
            while True:
                horizontal = rng.random() < 0.5
                x = rng.randrange(SIZE - (length - 1 if horizontal else 0))
                y = rng.randrange(SIZE - (0 if horizontal else length - 1))
                cells = ship_cells(x, y, length, horizontal)
                if self.fits(cells):
                    self.place(name, cells)
                    break

    def receive_shot(self, x, y):
        self.shots.add((x, y))
        ship = self.owner.get((x, y))
        if ship is None:
            return {"result": "miss"}
        ship.hits.add((x, y))
        if ship.sunk:
            return {"result": "sunk", "ship": ship.name, "cells": [list(c) for c in ship.cells]}
        return {"result": "hit"}

    def all_sunk(self):
        return bool(self.ships) and all(ship.sunk for ship in self.ships)


class Tracker:
    """What you know about the enemy's waters."""

    def __init__(self):
        self.grid = {}
        self.sunk = []
        self.revealed = {}  # cell -> ship name, filled in when the game ends

    def state(self, x, y):
        return self.grid.get((x, y), UNKNOWN)

    def record(self, x, y, result):
        kind = result["result"]
        if kind == "miss":
            self.grid[(x, y)] = MISS
        elif kind == "hit":
            self.grid[(x, y)] = HIT
        else:
            self.grid[(x, y)] = SUNK
            for cx, cy in result["cells"]:
                self.grid[(cx, cy)] = SUNK
            self.sunk.append(result["ship"])

    def remaining(self):
        return [(name, length) for name, length in FLEET if name not in self.sunk]


class Brain:
    """Picks shots by counting how many ways the remaining ships could cover each cell.

    Placements through known hits count far more, so after a hit it finishes the ship off.
    """

    def __init__(self, rng, view=None):
        self.rng = rng
        self.view = view or Tracker()

    def choose(self):
        grid = self.view.grid
        scores = {}
        for _, length in self.view.remaining():
            for horizontal in (True, False):
                for y in range(SIZE - (0 if horizontal else length - 1)):
                    for x in range(SIZE - (length - 1 if horizontal else 0)):
                        cells = ship_cells(x, y, length, horizontal)
                        states = [grid.get(c, UNKNOWN) for c in cells]
                        if MISS in states or SUNK in states:
                            continue
                        weight = 100 ** states.count(HIT)
                        for cell, state in zip(cells, states):
                            if state == UNKNOWN:
                                scores[cell] = scores.get(cell, 0) + weight
        if not scores:
            return self.rng.choice([(x, y) for y in range(SIZE) for x in range(SIZE) if (x, y) not in grid])
        best = max(scores.values())
        return self.rng.choice([cell for cell, score in scores.items() if score == best])


class Match:
    """One round, from your side. Messages from the opponent go through handle()."""

    def __init__(self, opponent, board, my_turn):
        self.opp = opponent
        self.board = board
        self.enemy = Tracker()
        self.my_turn = my_turn
        self.pending = None
        self.over = False
        self.won = False
        self.log = []
        self.shots = self.hits = 0
        if not my_turn:
            opponent.your_turn()

    def note(self, text, tone=""):
        self.log.append((text, tone))
        del self.log[:-200]

    def can_fire(self, x, y):
        return not self.over and self.my_turn and self.pending is None and self.enemy.state(x, y) == UNKNOWN

    def fire(self, x, y):
        if not self.can_fire(x, y):
            return False
        self.pending = (x, y)
        self.opp.send_shot(x, y)
        return True

    def handle(self, msg):
        kind = msg.get("type")
        if kind == "result":
            self._result(msg)
        elif kind == "shot":
            self._shot(msg)
        elif kind == "reveal":
            self._reveal(msg)
        else:
            raise ProtocolError(f"unexpected {kind!r} message")

    def _result(self, msg):
        if self.pending is None or (msg.get("x"), msg.get("y")) != self.pending:
            raise ProtocolError("got a result for a shot nobody fired")
        kind = msg.get("result")
        if kind not in ("miss", "hit", "sunk"):
            raise ProtocolError("got a nonsense result")
        if kind == "sunk":
            cells = msg.get("cells")
            if not isinstance(msg.get("ship"), str) or not isinstance(cells, list) or not all(
                isinstance(c, list) and len(c) == 2 and valid_cell(*c) for c in cells
            ):
                raise ProtocolError("got a nonsense result")
        x, y = self.pending
        self.pending = None
        self.enemy.record(x, y, msg)
        self.shots += 1
        if kind == "miss":
            self.note(f"You fire at {label(x, y)}. Miss.", "dim")
        elif kind == "hit":
            self.hits += 1
            self.note(f"You fire at {label(x, y)}. Hit!", "good")
        else:
            self.hits += 1
            self.note(f"You fire at {label(x, y)}. You sank their {clean(msg['ship'], 12)}!", "good")
        if msg.get("game_over"):
            self._finish(True)
        else:
            self.my_turn = False
            self.opp.your_turn()

    def _shot(self, msg):
        x, y = msg.get("x"), msg.get("y")
        if self.over or self.my_turn or not valid_cell(x, y) or (x, y) in self.board.shots:
            raise ProtocolError("they fired out of turn")
        result = self.board.receive_shot(x, y)
        result.update(type="result", x=x, y=y, game_over=self.board.all_sunk())
        self.opp.send_result(result)
        who = self.opp.name
        if result["result"] == "miss":
            self.note(f"{who} fires at {label(x, y)}. Miss.", "dim")
        elif result["result"] == "hit":
            self.note(f"{who} fires at {label(x, y)} and hits your {self.board.owner[(x, y)].name}.", "bad")
        else:
            self.note(f"{who} fires at {label(x, y)} and sinks your {result['ship']}.", "bad")
        if result["game_over"]:
            self._finish(False)
        else:
            self.my_turn = True

    def _finish(self, won):
        self.over, self.won, self.my_turn = True, won, False
        rate = round(100 * self.hits / self.shots) if self.shots else 0
        if won:
            self.note(f"You sank the whole fleet. You win! {self.shots} shots, {rate}% hits.", "good")
        else:
            self.note(f"{self.opp.name} sank your whole fleet. You lose.", "bad")
        self.opp.send_reveal(self.board)

    def _reveal(self, msg):
        ships = msg.get("ships")
        if not isinstance(ships, list):
            raise ProtocolError("got a nonsense reveal")
        for ship in ships:
            cells = ship.get("cells") if isinstance(ship, dict) else None
            if not isinstance(cells, list):
                raise ProtocolError("got a nonsense reveal")
            for cell in cells:
                if isinstance(cell, list) and len(cell) == 2 and valid_cell(*cell):
                    self.enemy.revealed[tuple(cell)] = ship.get("ship")


def clean(text, limit=16):
    if not isinstance(text, str):
        return ""
    return "".join(ch for ch in text if ch.isprintable()).strip()[:limit]


# --- Opponents --------------------------------------------------------------
# Both kinds have the same methods. Whatever they want to tell you comes out of poll().


class Computer:
    remote = False

    def __init__(self, rng, delay=AI_DELAY, clock=time.monotonic):
        self.name = "Computer"
        self.rng, self.delay, self.clock = rng, delay, clock
        self.events = []
        self.new_round()

    def new_round(self):
        self.board = Board()
        self.board.place_randomly(self.rng)
        self.brain = Brain(self.rng)
        self.due = None

    def poll(self):
        events, self.events = self.events, []
        if self.due is not None and self.clock() >= self.due:
            self.due = None
            x, y = self.brain.choose()
            events.append({"type": "shot", "x": x, "y": y})
        return events

    def your_turn(self):
        self.due = self.clock() + self.delay

    def send_ready(self):
        self.events.append({"type": "ready"})

    def send_shot(self, x, y):
        result = self.board.receive_shot(x, y)
        result.update(type="result", x=x, y=y, game_over=self.board.all_sunk())
        self.events.append(result)

    def send_result(self, result):
        self.brain.view.record(result["x"], result["y"], result)

    def send_reveal(self, board):
        ships = [{"ship": s.name, "cells": [list(c) for c in s.cells]} for s in self.board.ships]
        self.events.append({"type": "reveal", "ships": ships})

    def send_rematch(self):
        self.events.append({"type": "rematch"})

    def close(self):
        pass


class Remote:
    """A player on the other end of a TCP connection, speaking JSON lines."""

    remote = True

    def __init__(self, sock):
        sock.setblocking(True)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        self.sock = sock
        self.name = "Opponent"
        self.inbox = queue.Queue()
        self.alive = True
        self.closed = False
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            with self.sock.makefile("rb") as f:
                while True:
                    line = f.readline(MAX_LINE)
                    if not line:
                        break
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        msg = None
                    if not line.endswith(b"\n") or not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
                        self.inbox.put({"type": "error", "message": "They sent something this game doesn't understand."})
                        return
                    self.inbox.put(msg)
        except (OSError, ValueError):
            pass
        self.inbox.put({"type": "closed"})

    def poll(self):
        events = []
        while True:
            try:
                events.append(self.inbox.get_nowait())
            except queue.Empty:
                return events

    def send(self, msg):
        if not self.alive:
            return
        try:
            self.sock.sendall((json.dumps(msg) + "\n").encode("utf-8"))
        except OSError:
            self.alive = False
            self.inbox.put({"type": "closed"})

    def send_hello(self, name, host_first=None):
        msg = {"type": "hello", "version": PROTOCOL_VERSION, "name": name, "board_size": SIZE,
               "ships": [length for _, length in FLEET]}
        if host_first is not None:
            msg["host_first"] = host_first
        self.send(msg)

    def your_turn(self):
        pass

    def send_ready(self):
        self.send({"type": "ready"})

    def send_shot(self, x, y):
        self.send({"type": "shot", "x": x, "y": y})

    def send_result(self, result):
        self.send(result)

    def send_reveal(self, board):
        self.send({"type": "reveal", "ships": [{"ship": s.name, "cells": [list(c) for c in s.cells]} for s in board.ships]})

    def send_rematch(self):
        self.send({"type": "rematch"})

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.send({"type": "bye"})
        self.alive = False
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


class Listener:
    """Waits for one player to connect, in the background."""

    def __init__(self, host, port):
        self.server = socket.create_server((host, port))
        self.port = self.server.getsockname()[1]
        self.server.settimeout(0.2)
        self.conn = None
        self.cancelled = False
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while not self.cancelled:
            try:
                conn, _ = self.server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if self.cancelled:
                conn.close()
            else:
                self.conn = conn
            break
        self.server.close()

    def cancel(self):
        self.cancelled = True


class Connector:
    """Connects to a host in the background."""

    def __init__(self, host, port):
        self.sock = None
        self.error = None
        self.done = False
        self.cancelled = False
        threading.Thread(target=self._run, args=(host, port), daemon=True).start()

    def _run(self, host, port):
        try:
            sock = socket.create_connection((host, port), timeout=8)
        except ConnectionRefusedError:
            self.error = "Nobody is hosting a game there."
        except socket.timeout:
            self.error = "Timed out. Check the address, and that you're on the same network."
        except socket.gaierror:
            self.error = f"Couldn't find {host}."
        except OSError as e:
            self.error = f"Couldn't connect: {e.strerror or e}."
        else:
            sock.settimeout(None)
            if self.cancelled:
                sock.close()
            else:
                self.sock = sock
        self.done = True

    def cancel(self):
        self.cancelled = True


def local_ip():
    """The address other machines on your network would use to reach this one."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # no packets are sent for UDP connect
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def parse_address(text):
    text = text.strip()
    host, sep, port = text.rpartition(":")
    if not sep:
        host, port = text, str(DEFAULT_PORT)
    if not host or not port.isdigit() or not 0 < int(port) < 65536:
        return None
    return host, int(port)


# --- Terminal UI ------------------------------------------------------------

UNICODE = {"water": "·", "ship": "■", "miss": "•", "hit": "X", "pointer": "▸", "updown": "↑↓"}
ASCII = {"water": ".", "ship": "#", "miss": "o", "hit": "X", "pointer": ">", "updown": "Up/Down"}


class Style:
    def __init__(self, color=False):
        bold = curses.A_BOLD
        self.dim = getattr(curses, "A_DIM", 0)
        self.title = bold
        self.cursor = curses.A_REVERSE | bold
        self.water = self.miss = self.ship = self.reveal = 0
        self.accent = self.good = self.bad = self.hit = self.sunk = bold
        self.valid, self.invalid = bold, curses.A_REVERSE
        if not color:
            return
        background = -1
        try:
            curses.use_default_colors()
        except curses.error:
            background = curses.COLOR_BLACK
        pairs = {}
        for n, fg in enumerate((curses.COLOR_BLUE, curses.COLOR_RED, curses.COLOR_GREEN, curses.COLOR_YELLOW,
                                curses.COLOR_CYAN), start=1):
            curses.init_pair(n, fg, background)
            pairs[fg] = curses.color_pair(n)
        self.water = pairs[curses.COLOR_BLUE]
        self.hit = self.sunk = self.bad = pairs[curses.COLOR_RED] | bold
        self.good = self.valid = pairs[curses.COLOR_GREEN] | bold
        self.invalid = pairs[curses.COLOR_RED] | bold
        self.accent = pairs[curses.COLOR_YELLOW] | bold
        self.reveal = pairs[curses.COLOR_YELLOW]
        self.ship = pairs[curses.COLOR_CYAN] | bold

    @classmethod
    def for_terminal(cls):
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        color = curses.has_colors()
        if color:
            curses.start_color()
        return cls(color)


def keyset(*names, chars=""):
    keys = {getattr(curses, n) for n in names if hasattr(curses, n)}
    return keys | {ord(c) for c in chars}


class App:
    BOARD_W = 3 + SIZE * 2

    def __init__(self, win, args, style=None, clock=time.monotonic, ai_delay=AI_DELAY):
        self.win, self.args, self.clock, self.ai_delay = win, args, clock, ai_delay
        self.style = style or Style(False)
        self.g = ASCII if args.ascii else UNICODE
        self.name = clean(args.name or default_name()) or "Player"
        self.rng = random.Random(args.seed)
        self.k_up = keyset("KEY_UP", chars="wW")
        self.k_down = keyset("KEY_DOWN", chars="sS")
        self.k_left = keyset("KEY_LEFT", chars="aA")
        self.k_right = keyset("KEY_RIGHT", chars="dD")
        self.k_enter = keyset("KEY_ENTER", "PADENTER", chars="\n\r")
        self.k_back = keyset("KEY_BACKSPACE", "KEY_DC", chars="\b\x7f")
        self.k_quit = {27, ord("q"), ord("Q")}

        self.state = "menu"
        self.menu_index = 0
        self.text = ""
        self.message = ""
        self.flash = None
        self.opp = self.listener = self.connector = self.match = None
        self.board = Board()
        self.cursor = (0, 0)
        self.horizontal = True
        self.aim = (0, 0)
        self.i_first = False
        self.peer_ready = False
        self.want_rematch = self.peer_rematch = self.peer_gone = False
        self.is_host = self.host_first = False
        self.quit_armed = False
        self.demo_next = 0.0
        self.over_at = 0.0

        if args.demo or args.computer:
            self.start_computer()
        elif args.host:
            self.start_host()
        elif args.join:
            self.start_join(args.join)

    # --- main loop ---

    def run(self):
        self.win.keypad(True)
        self.win.timeout(100)
        try:
            while self.state != "quit":
                self.pump()
                self.tick()
                self.draw()
                key = self.win.getch()
                if key == -1:
                    continue
                if key == getattr(curses, "KEY_RESIZE", None):
                    if sys.platform == "win32":
                        curses.resize_term(0, 0)
                    continue
                self.on_key(key)
        finally:
            self.drop()

    def pump(self):
        if self.state == "hosting" and self.listener.conn:
            self.opp = Remote(self.listener.conn)
            self.listener = None
            self.opp.send_hello(self.name, self.host_first)
            self.state = "handshake"
        elif self.state == "connecting" and self.connector.done:
            if self.connector.error:
                self.notice(self.connector.error)
            else:
                self.opp = Remote(self.connector.sock)
                self.opp.send_hello(self.name)
                self.state = "handshake"
            self.connector = None
        if self.opp is None:
            return
        for msg in self.opp.poll():
            if self.opp is None:
                break
            try:
                self.on_message(msg)
            except ProtocolError as e:
                self.drop()
                self.notice(f"The game was cut short: {e}.")

    def on_message(self, msg):
        kind = msg.get("type")
        if kind in ("closed", "bye", "error"):
            if self.state == "over":
                self.peer_gone = True
                self.opp.close()
            elif self.state == "handshake" and kind == "closed":
                self.drop()
                self.notice("The connection closed before the game started.")
            else:
                name = self.opp.name
                self.drop()
                self.notice(msg.get("message") or f"{name} left the game.")
        elif kind == "hello" and self.state == "handshake":
            self.on_hello(msg)
        elif kind == "ready" and self.state in ("placing", "ready"):
            self.peer_ready = True
            if self.state == "ready":
                self.begin_battle()
        elif kind == "rematch" and self.state == "over":
            self.peer_rematch = True
            if self.want_rematch:
                self.new_round()
        elif kind in ("shot", "result", "reveal") and self.state in ("battle", "over"):
            self.match.handle(msg)
            if self.match.over and self.state == "battle":
                self.state = "over"
                self.over_at = self.clock()
                self.want_rematch = self.peer_rematch = self.peer_gone = False
        else:
            raise ProtocolError(f"got an unexpected {kind!r} message")

    def on_hello(self, msg):
        if msg.get("version") != PROTOCOL_VERSION:
            raise ProtocolError("they're running a different version of this game")
        if msg.get("board_size") != SIZE or msg.get("ships") != [length for _, length in FLEET]:
            raise ProtocolError("their game uses different rules")
        self.opp.name = clean(msg.get("name")) or "Opponent"
        if self.is_host:
            self.i_first = self.host_first
        else:
            if not isinstance(msg.get("host_first"), bool):
                raise ProtocolError("the host didn't say who goes first")
            self.i_first = not msg["host_first"]
        self.begin_placing()

    def tick(self):
        now = self.clock()
        if self.flash and now > self.flash[1]:
            self.flash = None
        if not self.args.demo or now < self.demo_next:
            return
        self.demo_next = now + 0.3
        if self.state == "placing":
            self.board.place_randomly(self.rng)
            self.confirm_placement()
        elif self.state == "battle" and self.match.my_turn and self.match.pending is None:
            self.aim = Brain(self.rng, self.match.enemy).choose()
            self.match.fire(*self.aim)
        elif self.state == "over" and now - self.over_at > 4:
            self.request_rematch()

    # --- transitions ---

    def say(self, text, seconds=2.5):
        self.flash = (text, self.clock() + seconds)

    def notice(self, text):
        self.state = "notice"
        self.message = text

    def drop(self):
        """Close any connection and forget the current game."""
        if self.opp:
            self.opp.close()
        for waiter in (self.listener, self.connector):
            if waiter:
                waiter.cancel()
        self.opp = self.listener = self.connector = self.match = None

    def to_menu(self):
        self.drop()
        self.state = "menu"

    def start_computer(self):
        self.drop()
        self.opp = Computer(self.rng, self.ai_delay, self.clock)
        self.i_first = self.rng.random() < 0.5
        self.begin_placing()

    def start_host(self):
        self.drop()
        try:
            self.listener = Listener(self.args.bind, self.args.port)
        except OSError as e:
            return self.notice(f"Couldn't host on port {self.args.port}: {e.strerror or e}.")
        self.is_host = True
        self.host_first = self.rng.random() < 0.5
        self.address = f"{local_ip()}:{self.listener.port}"
        self.state = "hosting"

    def start_join(self, text):
        address = parse_address(text)
        if not address:
            return self.notice(f"{text!r} doesn't look like an address. Try something like 192.168.1.20:{DEFAULT_PORT}.")
        self.drop()
        self.is_host = False
        self.address = f"{address[0]}:{address[1]}"
        self.connector = Connector(*address)
        self.state = "connecting"

    def begin_placing(self):
        self.board = Board()
        self.cursor = (0, 0)
        self.horizontal = True
        self.peer_ready = False
        self.state = "placing"

    def confirm_placement(self):
        self.state = "ready"
        self.opp.send_ready()
        if self.peer_ready:
            self.begin_battle()

    def begin_battle(self):
        self.match = Match(self.opp, self.board, self.i_first)
        self.match.note("You go first." if self.i_first else f"{self.opp.name} goes first.")
        self.quit_armed = False
        self.state = "battle"

    def request_rematch(self):
        if self.peer_gone or self.want_rematch:
            return
        self.want_rematch = True
        self.opp.send_rematch()
        if self.peer_rematch:
            self.new_round()

    def new_round(self):
        self.i_first = not self.i_first
        if not self.opp.remote:
            self.opp.new_round()
        self.match = None
        self.begin_placing()

    # --- keys ---

    def on_key(self, key):
        handler = getattr(self, f"key_{self.state}", None)
        if handler:
            handler(key)

    def move(self, key, pos, length=1, horizontal=True):
        x, y = pos
        if key in self.k_up:
            y -= 1
        elif key in self.k_down:
            y += 1
        elif key in self.k_left:
            x -= 1
        elif key in self.k_right:
            x += 1
        else:
            return None
        return clamp((x, y), length, horizontal)

    MENU = ["Play the computer", "Host a network game", "Join a network game", "Quit"]

    def key_menu(self, key):
        if key in self.k_up:
            self.menu_index = (self.menu_index - 1) % len(self.MENU)
        elif key in self.k_down:
            self.menu_index = (self.menu_index + 1) % len(self.MENU)
        elif key in self.k_quit:
            self.state = "quit"
        elif ord("1") <= key <= ord("4"):
            self.menu_index = key - ord("1")
            self.key_menu(10)
        elif key in self.k_enter:
            choice = self.menu_index
            if choice == 0:
                self.start_computer()
            elif choice == 1:
                self.start_host()
            elif choice == 2:
                self.state = "join"
            else:
                self.state = "quit"

    def key_join(self, key):
        if key == 27:
            self.state = "menu"
        elif key in self.k_enter:
            if self.text.strip():
                self.start_join(self.text)
        elif key in self.k_back:
            self.text = self.text[:-1]
        elif 32 < key < 127 and len(self.text) < 60:
            self.text += chr(key)

    def key_hosting(self, key):
        if key in self.k_quit:
            self.to_menu()

    key_connecting = key_handshake = key_ready = key_hosting

    def key_placing(self, key):
        nxt = self.board.next_ship()
        length = nxt[1] if nxt else 1
        if key in self.k_quit:
            return self.to_menu()
        if key == ord(" "):
            self.board.place_randomly(self.rng)
        elif key in self.k_back or key in (ord("u"), ord("U")):
            self.board.undo()
        elif not nxt:
            if key in self.k_enter:
                self.confirm_placement()
        elif key in (ord("r"), ord("R")):
            self.horizontal = not self.horizontal
            self.cursor = clamp(self.cursor, length, self.horizontal)
        elif key in self.k_enter:
            cells = ship_cells(*self.cursor, length, self.horizontal)
            if self.board.fits(cells):
                self.board.place(nxt[0], cells)
            else:
                self.say("That overlaps another ship.")
        else:
            self.cursor = self.move(key, self.cursor, length, self.horizontal) or self.cursor

    def key_battle(self, key):
        if key in self.k_quit:
            if self.quit_armed:
                return self.to_menu()
            self.quit_armed = True
            return self.say("Press Q again to leave the game.", 3)
        self.quit_armed = False
        if key in self.k_enter or key == ord(" "):
            x, y = self.aim
            if self.match.enemy.state(x, y) != UNKNOWN:
                self.say("You've already fired there.")
            elif not self.match.my_turn:
                self.say(f"Wait for {self.opp.name}.")
            else:
                self.match.fire(x, y)
        else:
            self.aim = self.move(key, self.aim) or self.aim

    def key_over(self, key):
        if key in (ord("n"), ord("N")):
            self.request_rematch()
        elif key in self.k_quit:
            self.to_menu()

    def key_notice(self, key):
        self.to_menu()

    # --- drawing ---

    def put(self, y, x, text, attr=0):
        h, w = self.win.getmaxyx()
        if y < 0 or y >= h or x >= w:
            return
        text = text[: max(0, w - x - (1 if y == h - 1 else 0))]
        if text:
            try:
                self.win.addstr(y, x, text, attr)
            except curses.error:
                pass

    def draw(self):
        self.win.erase()
        h, w = self.win.getmaxyx()
        if w < MIN_W or h < MIN_H:
            self.put(0, 0, f"Make the window a bit bigger ({MIN_W}x{MIN_H} or more).")
        else:
            self.put(0, 2, "BATTLESHIP", self.style.title)
            if self.opp and self.state not in ("handshake",):
                versus = f"{self.name} vs {self.opp.name}"
                self.put(0, max(14, w - len(versus) - 2), versus, self.style.dim)
            getattr(self, f"draw_{self.state}", lambda: None)()
        self.win.refresh()

    def status(self, text, attr=0, keys=""):
        h, w = self.win.getmaxyx()
        if self.flash:
            text, attr = self.flash[0], self.style.accent
        self.put(h - 1, 2, text, attr)
        if keys and len(text) + len(keys) + 6 < w:
            self.put(h - 1, w - len(keys) - 2, keys, self.style.dim)

    def lines(self, top, rows, left=2):
        for i, row in enumerate(rows):
            text, attr = row if isinstance(row, tuple) else (row, 0)
            self.put(top + i, left, text, attr)

    def draw_menu(self):
        s, g = self.style, self.g
        items = [(f"{g['pointer']} {item}", s.accent) if i == self.menu_index else (f"  {item}", 0)
                 for i, item in enumerate(self.MENU)]
        self.lines(2, ["Sink the enemy fleet before they sink yours.", ""] + items)
        self.status(f"{g['updown']} choose   Enter select   Q quit")

    def draw_join(self):
        self.lines(2, [("Join a network game", self.style.title), "",
                       "Type the address the host sees on their screen.", "",
                       (f"Address: {self.text}_", self.style.accent), "",
                       (f"The port is optional (default {DEFAULT_PORT}).", self.style.dim)])
        self.status("Enter connect   Esc back")

    def draw_hosting(self):
        host = self.address.split(":")[0]
        port_flag = "" if self.listener.port == DEFAULT_PORT else f":{self.listener.port}"
        self.lines(2, [("Hosting a game", self.style.title), "",
                       "Waiting for someone to join. They should pick", "\"Join a network game\" and type:", "",
                       (f"    {self.address}", self.style.accent), "",
                       ("or run:", self.style.dim),
                       (f"    python battleship.py --join {host}{port_flag}", self.style.dim)])
        self.status("Waiting...", keys="Esc cancel")

    def draw_connecting(self):
        self.lines(2, [("Joining a game", self.style.title), "", f"Connecting to {self.address}..."])
        self.status("", keys="Esc cancel")

    def draw_handshake(self):
        self.lines(2, [("Connected", self.style.title), "", "Getting ready..."])
        self.status("", keys="Esc cancel")

    def draw_notice(self):
        self.lines(2, wrap(self.message, self.win.getmaxyx()[1] - 4))
        self.status("Press any key to go back to the menu.")

    def board_rows(self, top, left, title, cell, cursor=None):
        s = self.style
        self.put(top, left, title, s.title)
        self.put(top + 1, left + 3, " ".join(str(i + 1) for i in range(SIZE)), s.dim)
        for y in range(SIZE):
            self.put(top + 2 + y, left, ROWS[y], s.dim)
            for x in range(SIZE):
                ch, attr = cell(x, y)
                if cursor == (x, y):
                    attr = s.cursor
                self.put(top + 2 + y, left + 3 + x * 2, ch, attr)

    def own_cell(self, preview=(), preview_ok=True):
        g, s, board = self.g, self.style, self.board

        def cell(x, y):
            if (x, y) in preview:
                return g["ship"], s.valid if preview_ok else s.invalid
            ship, shot = board.owner.get((x, y)), (x, y) in board.shots
            if ship:
                return (g["hit"], s.hit) if shot else (g["ship"], s.ship)
            return (g["miss"], s.miss) if shot else (g["water"], s.water)
        return cell

    def enemy_cell(self, x, y):
        g, s, enemy = self.g, self.style, self.match.enemy
        state = enemy.state(x, y)
        if state == MISS:
            return g["miss"], s.miss
        if state == HIT:
            return g["hit"], s.hit
        if state == SUNK:
            return g["ship"], s.sunk
        if (x, y) in enemy.revealed:
            return g["ship"], s.reveal
        return g["water"], s.water

    def draw_placing(self):
        s, g = self.style, self.g
        nxt = self.board.next_ship()
        preview, ok = (), True
        if nxt and self.state == "placing":
            preview = ship_cells(*self.cursor, nxt[1], self.horizontal)
            ok = self.board.fits(preview)
        self.board_rows(2, 2, "Your fleet", self.own_cell(preview, ok))
        right = 2 + self.BOARD_W + 6
        if self.state == "ready":
            rows = [("Fleet ready", s.title), "", f"Waiting for {self.opp.name}", "to place their ships..."]
            self.lines(2, rows, right)
            return self.status("", keys="Esc leave")
        rows = [("Place your ships", s.title), ""]
        for i, (name, length) in enumerate(FLEET):
            text = f"{name:<11}{g['ship'] * length}"
            if nxt and name == nxt[0]:
                rows.append((f"{g['pointer']} {text}", s.accent))
            else:
                rows.append((f"  {text}", s.dim if i < len(self.board.ships) else 0))
        rows.append("")
        if nxt:
            rows += ["Arrows  move", "R       rotate", "Enter   place", "Bksp    undo", "Space   random layout"]
        else:
            rows += [("Enter   start the battle", s.accent), "Bksp    undo", "Space   shuffle"]
        self.lines(2, rows, right)
        self.status("", keys="Esc leave")

    draw_ready = draw_placing

    def draw_battle(self):
        h, w = self.win.getmaxyx()
        s, g, m = self.style, self.g, self.match
        right = 2 + self.BOARD_W + 8
        playing = self.state == "battle" and m.my_turn and m.pending is None
        self.board_rows(2, 2, "Your fleet", self.own_cell())
        self.board_rows(2, right, f"{self.opp.name}'s waters", self.enemy_cell, self.aim if playing else None)
        y = 15
        if h >= y + len(FLEET) + 3:
            for i, ship in enumerate(self.board.ships):
                marks = "".join(g["hit"] if c in ship.hits else g["ship"] for c in ship.cells)
                self.put(y + i, 2, f"{ship.name:<11}{marks}", s.dim if ship.sunk else 0)
            for i, (name, length) in enumerate(FLEET):
                sunk = name in m.enemy.sunk
                self.put(y + i, right, f"{name:<11}{(g['hit'] if sunk else g['ship']) * length}", s.dim if sunk else 0)
            y += len(FLEET) + 1
        rows = max(1, h - 1 - y)
        tones = {"good": s.good, "bad": s.bad, "dim": s.dim}
        for i, (text, tone) in enumerate(m.log[-rows:]):
            self.put(y + i, 2, text, tones.get(tone, 0))
        if self.state == "over":
            return self.draw_over_status()
        if playing:
            self.status(f"Your turn. Aim at {label(*self.aim)} and press Enter.", s.accent, "Q leave")
        elif m.pending:
            self.status("Firing...", keys="Q leave")
        else:
            self.status(f"{self.opp.name} is aiming...", keys="Q leave")

    draw_over = draw_battle

    def draw_over_status(self):
        s, name = self.style, self.opp.name
        if self.peer_gone:
            self.status(f"{name} left.", keys="Q menu")
        elif self.want_rematch:
            self.status(f"Waiting for {name} to accept the rematch...", keys="Q menu")
        elif self.peer_rematch:
            self.status(f"{name} wants a rematch. Press N to play again.", s.accent, "Q menu")
        else:
            self.status("N rematch", s.accent, "Q menu")


def wrap(text, width):
    out, line = [], ""
    for word in text.split():
        if line and len(line) + 1 + len(word) > width:
            out.append(line)
            line = word
        else:
            line = f"{line} {word}" if line else word
    return out + [line]


def default_name():
    try:
        return getpass.getuser()
    except Exception:
        return "Player"


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Battleship in your terminal. Play the computer, or a friend over the network.")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--computer", action="store_true", help="play against the computer")
    mode.add_argument("--host", action="store_true", help="host a network game")
    mode.add_argument("--join", "--connect", metavar="ADDRESS", help="join a game at HOST or HOST:PORT")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port to host on (default {DEFAULT_PORT})")
    p.add_argument("--name", help="your name (default: your username)")
    p.add_argument("--ascii", action="store_true", help="use plain ASCII symbols")
    p.add_argument("--bind", default="", help=argparse.SUPPRESS)
    p.add_argument("--seed", type=int, help=argparse.SUPPRESS)
    p.add_argument("--demo", action="store_true", help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    if not 0 < args.port < 65536:
        p.error("--port must be between 1 and 65535")
    return args


def main(argv=None):
    args = parse_args(argv)
    if curses is None:
        print("This game needs curses. On Windows, install it with:\n\n    python -m pip install windows-curses\n")
        return 1
    os.environ.setdefault("ESCDELAY", "25")
    locale.setlocale(locale.LC_ALL, "")
    try:
        curses.wrapper(lambda screen: App(screen, args, Style.for_terminal()).run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
