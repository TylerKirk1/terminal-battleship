#!/usr/bin/env python3
import argparse
import curses
import json
import random
import socket
import sys
import time

BOARD_SIZE = 10
SHIPS = [5, 4, 3, 3, 2]
PROTOCOL_VERSION = 1

STATE_UNKNOWN = 0
STATE_MISS = 1
STATE_HIT = 2
STATE_SUNK = 3


class JsonLineSocket:
    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.file = sock.makefile("rwb")

    def send(self, obj):
        data = (json.dumps(obj) + "\n").encode("utf-8")
        self.file.write(data)
        self.file.flush()

    def recv(self):
        line = self.file.readline()
        if not line:
            raise ConnectionError("connection closed")
        return json.loads(line.decode("utf-8"))

    def close(self):
        try:
            self.file.close()
        finally:
            self.sock.close()


class Ship:
    def __init__(self, ship_id, size, cells):
        self.ship_id = ship_id
        self.size = size
        self.cells = set(cells)
        self.hits = set()

    def register_hit(self, pos):
        if pos in self.cells:
            self.hits.add(pos)

    def is_sunk(self):
        return len(self.hits) == self.size


class Board:
    def __init__(self, size, ship_sizes):
        self.size = size
        self.ship_sizes = ship_sizes
        self.grid = [[{"ship": None, "hit": False} for _ in range(size)] for _ in range(size)]
        self.ships = {}

    def in_bounds(self, x, y):
        return 0 <= x < self.size and 0 <= y < self.size

    def place_random(self, rng):
        ship_id = 0
        for size in self.ship_sizes:
            placed = False
            attempts = 0
            while not placed:
                attempts += 1
                if attempts > 1000:
                    raise RuntimeError("failed to place ships")
                horizontal = rng.choice([True, False])
                if horizontal:
                    x = rng.randint(0, self.size - size)
                    y = rng.randint(0, self.size - 1)
                    cells = [(x + i, y) for i in range(size)]
                else:
                    x = rng.randint(0, self.size - 1)
                    y = rng.randint(0, self.size - size)
                    cells = [(x, y + i) for i in range(size)]
                if all(self.grid[cy][cx]["ship"] is None for cx, cy in cells):
                    for cx, cy in cells:
                        self.grid[cy][cx]["ship"] = ship_id
                    self.ships[ship_id] = Ship(ship_id, size, cells)
                    ship_id += 1
                    placed = True

    def receive_shot(self, x, y):
        cell = self.grid[y][x]
        if cell["hit"]:
            return "repeat", None
        cell["hit"] = True
        ship_id = cell["ship"]
        if ship_id is None:
            return "miss", None
        ship = self.ships[ship_id]
        ship.register_hit((x, y))
        if ship.is_sunk():
            return "sunk", ship.size
        return "hit", None

    def all_sunk(self):
        return all(ship.is_sunk() for ship in self.ships.values())


class OpponentTracker:
    def __init__(self, size):
        self.size = size
        self.grid = [[STATE_UNKNOWN for _ in range(size)] for _ in range(size)]

    def mark(self, x, y, result):
        if result == "miss":
            self.grid[y][x] = STATE_MISS
        elif result == "hit":
            self.grid[y][x] = STATE_HIT
        elif result == "sunk":
            self.grid[y][x] = STATE_SUNK

    def was_shot(self, x, y):
        return self.grid[y][x] != STATE_UNKNOWN


class Game:
    def __init__(self, socket_line, is_host, name, peer_name, rng):
        self.socket_line = socket_line
        self.is_host = is_host
        self.name = name
        self.peer_name = peer_name
        self.rng = rng
        self.board = Board(BOARD_SIZE, SHIPS)
        self.board.place_random(rng)
        self.opponent = OpponentTracker(BOARD_SIZE)
        self.my_turn = False
        self.game_over = False
        self.winner = None

    def run(self, stdscr):
        curses.curs_set(0)
        stdscr.nodelay(False)
        stdscr.keypad(True)
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_GREEN, -1)
            curses.init_pair(2, curses.COLOR_RED, -1)
            curses.init_pair(3, curses.COLOR_BLUE, -1)
            curses.init_pair(4, curses.COLOR_YELLOW, -1)

        cursor_x, cursor_y = 0, 0

        while not self.game_over:
            self.draw(stdscr, cursor_x, cursor_y)
            if self.my_turn:
                key = stdscr.getch()
                if key in (ord("q"), 27):
                    self.game_over = True
                    self.winner = None
                    break
                if key in (curses.KEY_LEFT, ord("a")):
                    cursor_x = (cursor_x - 1) % BOARD_SIZE
                elif key in (curses.KEY_RIGHT, ord("d")):
                    cursor_x = (cursor_x + 1) % BOARD_SIZE
                elif key in (curses.KEY_UP, ord("w")):
                    cursor_y = (cursor_y - 1) % BOARD_SIZE
                elif key in (curses.KEY_DOWN, ord("s")):
                    cursor_y = (cursor_y + 1) % BOARD_SIZE
                elif key in (curses.KEY_ENTER, 10, 13, ord(" ")):
                    if self.opponent.was_shot(cursor_x, cursor_y):
                        curses.beep()
                        continue
                    self.socket_line.send({"type": "shot", "x": cursor_x, "y": cursor_y})
                    msg = self.socket_line.recv()
                    if msg.get("type") != "result":
                        raise RuntimeError("protocol error: expected result")
                    result = msg["result"]
                    self.opponent.mark(cursor_x, cursor_y, result)
                    if msg.get("game_over"):
                        self.game_over = True
                        self.winner = self.name
                        break
                    self.my_turn = False
            else:
                self.draw_status(stdscr, "Waiting for opponent...")
                stdscr.refresh()
                msg = self.socket_line.recv()
                if msg.get("type") != "shot":
                    raise RuntimeError("protocol error: expected shot")
                x, y = msg["x"], msg["y"]
                result, ship_size = self.board.receive_shot(x, y)
                if result == "repeat":
                    result = "miss"
                game_over = self.board.all_sunk()
                self.socket_line.send({
                    "type": "result",
                    "x": x,
                    "y": y,
                    "result": result,
                    "ship_size": ship_size,
                    "game_over": game_over,
                })
                if game_over:
                    self.game_over = True
                    self.winner = self.peer_name
                    break
                self.my_turn = True

        self.draw(stdscr, cursor_x, cursor_y)
        if self.winner is None:
            self.draw_status(stdscr, "Game ended.")
        elif self.winner == self.name:
            self.draw_status(stdscr, "You win!")
        else:
            self.draw_status(stdscr, "You lose.")
        stdscr.refresh()
        time.sleep(2)

    def draw(self, stdscr, cursor_x, cursor_y):
        stdscr.clear()
        max_y, max_x = stdscr.getmaxyx()
        left_x = 2
        top_y = 1
        gap = 6
        board_width = 3 + BOARD_SIZE * 3
        right_x = left_x + board_width + gap

        if right_x + board_width + 2 > max_x or top_y + BOARD_SIZE + 4 > max_y:
            stdscr.addstr(0, 0, "Terminal too small. Resize and try again.")
            stdscr.refresh()
            return

        self.draw_board(
            stdscr,
            top_y,
            left_x,
            title=f"{self.name} (You)",
            show_ships=True,
            cursor=None,
        )
        self.draw_board(
            stdscr,
            top_y,
            right_x,
            title=f"{self.peer_name} (Opponent)",
            show_ships=False,
            cursor=(cursor_x, cursor_y),
        )

        status = "Your turn" if self.my_turn else "Opponent's turn"
        self.draw_status(stdscr, status + " | Arrows/WASD move, Enter/Space fire, Q/Esc quit")
        stdscr.refresh()

    def draw_status(self, stdscr, text):
        max_y, _ = stdscr.getmaxyx()
        stdscr.addstr(max_y - 2, 2, text[:200])

    def draw_board(self, stdscr, top, left, title, show_ships, cursor):
        stdscr.addstr(top, left, title)
        header_row = top + 1
        stdscr.addstr(header_row, left + 3, "")
        for x in range(BOARD_SIZE):
            stdscr.addstr(header_row, left + 3 + x * 3, f"{x + 1:2d}")
        for y in range(BOARD_SIZE):
            row_y = header_row + 1 + y
            stdscr.addstr(row_y, left, chr(65 + y))
            for x in range(BOARD_SIZE):
                cell_x = left + 3 + x * 3
                ch, attr = self.cell_display(x, y, show_ships)
                if cursor and (x, y) == cursor and self.my_turn and not show_ships:
                    attr |= curses.A_REVERSE
                stdscr.addstr(row_y, cell_x, f" {ch}", attr)

    def cell_display(self, x, y, show_ships):
        attr = curses.A_NORMAL
        if show_ships:
            cell = self.board.grid[y][x]
            ship_id = cell["ship"]
            if ship_id is None:
                if cell["hit"]:
                    ch = "*"
                    attr = self.color_attr(3)
                else:
                    ch = "."
            else:
                if cell["hit"]:
                    ch = "X"
                    attr = self.color_attr(2)
                else:
                    ch = "O"
                    attr = self.color_attr(1)
        else:
            state = self.opponent.grid[y][x]
            if state == STATE_UNKNOWN:
                ch = "."
            elif state == STATE_MISS:
                ch = "*"
                attr = self.color_attr(3)
            elif state == STATE_HIT:
                ch = "X"
                attr = self.color_attr(2)
            else:
                ch = "S"
                attr = self.color_attr(2) | curses.A_BOLD
        return ch, attr

    def color_attr(self, pair_id):
        if curses.has_colors():
            return curses.color_pair(pair_id)
        return curses.A_NORMAL


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Terminal Battleship over LAN")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--host", action="store_true", help="Host a game")
    mode.add_argument("--connect", metavar="HOST:PORT", help="Connect to a host")
    parser.add_argument("--port", type=int, default=9999, help="Port to listen on (host mode)")
    parser.add_argument("--name", default=None, help="Player name")
    return parser.parse_args(argv)


def exchange_hello(sock_line, name, is_host):
    hello = {
        "type": "hello",
        "version": PROTOCOL_VERSION,
        "name": name,
        "board_size": BOARD_SIZE,
        "ships": SHIPS,
    }
    if is_host:
        sock_line.send(hello)
        peer = sock_line.recv()
    else:
        peer = sock_line.recv()
        sock_line.send(hello)

    if peer.get("type") != "hello":
        raise RuntimeError("protocol error: expected hello")
    if peer.get("version") != PROTOCOL_VERSION:
        raise RuntimeError("protocol error: version mismatch")
    if peer.get("board_size") != BOARD_SIZE or peer.get("ships") != SHIPS:
        raise RuntimeError("protocol error: mismatched settings")
    return peer.get("name", "Opponent")


def exchange_ready(sock_line, is_host):
    sock_line.send({"type": "ready"})
    if is_host:
        msg = sock_line.recv()
        if msg.get("type") != "ready":
            raise RuntimeError("protocol error: expected ready")


def exchange_start(sock_line, is_host, rng):
    if is_host:
        host_goes_first = rng.choice([True, False])
        sock_line.send({"type": "start", "you_go_first": not host_goes_first})
        return host_goes_first
    msg = sock_line.recv()
    if msg.get("type") != "start":
        raise RuntimeError("protocol error: expected start")
    return msg.get("you_go_first", False)


def run_game(socket_line, is_host, name, peer_name, rng):
    def wrapped(stdscr):
        game = Game(socket_line, is_host, name, peer_name, rng)
        exchange_ready(socket_line, is_host)
        game.my_turn = exchange_start(socket_line, is_host, rng)
        game.run(stdscr)
    curses.wrapper(wrapped)


def main(argv):
    args = parse_args(argv)
    name = args.name or ("Host" if args.host else "Client")

    if args.host:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("0.0.0.0", args.port))
        server.listen(1)
        print(f"Hosting on port {args.port}. Waiting for connection...")
        conn, addr = server.accept()
        print(f"Connected from {addr[0]}:{addr[1]}")
        sock_line = JsonLineSocket(conn)
        peer_name = exchange_hello(sock_line, name, is_host=True)
        rng = random.Random()
        run_game(sock_line, is_host=True, name=name, peer_name=peer_name, rng=rng)
        sock_line.close()
        server.close()
    else:
        host, port = args.connect.split(":")
        port = int(port)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect((host, port))
        sock_line = JsonLineSocket(sock)
        peer_name = exchange_hello(sock_line, name, is_host=False)
        rng = random.Random()
        run_game(sock_line, is_host=False, name=name, peer_name=peer_name, rng=rng)
        sock_line.close()


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except KeyboardInterrupt:
        print("\nExiting.")
        sys.exit(0)
