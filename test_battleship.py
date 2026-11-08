"""Run with: python -m unittest"""

import argparse
import json
import random
import socket
import threading
import time
import unittest

import battleship as bs

curses = bs.curses


def make_args(**overrides):
    args = dict(computer=False, host=False, join=None, port=bs.DEFAULT_PORT, name="Ann", ascii=False,
                bind="127.0.0.1", seed=1, demo=False)
    args.update(overrides)
    return argparse.Namespace(**args)


class FakeWin:
    """Stands in for a curses window: records text into a grid and asks a bot for keys."""

    def __init__(self, keys, h=30, w=80, limit=30):
        self.keys, self.h, self.w = keys, h, w
        self.deadline = time.monotonic() + limit
        self.erase()

    def getmaxyx(self):
        return self.h, self.w

    def erase(self):
        self.cells = [[" "] * self.w for _ in range(self.h)]

    def addstr(self, y, x, text, attr=0):
        if not (0 <= y < self.h and 0 <= x and x + len(text) <= self.w):
            raise curses.error("write outside the window")
        self.cells[y][x:x + len(text)] = list(text)

    def refresh(self):
        self.screen = "\n".join("".join(row).rstrip() for row in self.cells)

    def keypad(self, flag):
        pass

    def timeout(self, ms):
        pass

    def getch(self):
        if time.monotonic() > self.deadline:
            raise TimeoutError("the game didn't finish in time")
        key = self.keys()
        if key == -1:
            time.sleep(0.002)
        return key


class Bot:
    """Plays through the real UI with keypresses: random fleet, then walks the cursor to good shots."""

    def __init__(self, seed=0, rematches=0, leave_after=None):
        self.rng = random.Random(seed)
        self.rematches = rematches
        self.leave_after = leave_after
        self.results = []
        self.notices = []
        self.target = None
        self.app = None
        self.seen = []

    def __call__(self):
        app = self.app
        state = app.state
        if state == "placing":
            return ord(" ") if not app.board.complete else 10
        if state == "battle":
            m = app.match
            if self.leave_after is not None and m.shots >= self.leave_after:
                return ord("q")
            if not m.my_turn or m.pending:
                return -1
            if self.target is None:
                self.target = bs.Brain(self.rng, m.enemy).choose()
            (ax, ay), (tx, ty) = app.aim, self.target
            if ax != tx:
                return curses.KEY_RIGHT if ax < tx else curses.KEY_LEFT
            if ay != ty:
                return curses.KEY_DOWN if ay < ty else curses.KEY_UP
            self.target = None
            return 10
        if state == "over":
            if not any(m is app.match for m in self.seen):
                self.seen.append(app.match)
                self.results.append((app.match.won, app.match.shots, app.i_first, app.win.screen))
            if app.want_rematch:
                return -1
            if self.rematches:
                self.rematches -= 1
                return ord("n")
            return ord("q")
        if state == "notice":
            self.notices.append(app.message)
            return 10
        if state == "menu":
            return ord("q")
        return -1


def play(args, bot, **app_kwargs):
    win = FakeWin(bot, **{k: app_kwargs.pop(k) for k in ("h", "w") if k in app_kwargs})
    app = bs.App(win, args, ai_delay=0, **app_kwargs)
    bot.app = app
    app.run()
    return app


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise TimeoutError
        time.sleep(0.01)


class BoardTests(unittest.TestCase):
    def test_place_and_overlap(self):
        board = bs.Board()
        board.place("Carrier", bs.ship_cells(0, 0, 5, True))
        self.assertFalse(board.fits(bs.ship_cells(4, 0, 3, False)))
        self.assertFalse(board.fits(bs.ship_cells(8, 5, 3, True)))
        self.assertTrue(board.fits(bs.ship_cells(5, 0, 3, True)))
        board.undo()
        self.assertEqual(board.owner, {})

    def test_shots(self):
        board = bs.Board()
        board.place("Destroyer", [(3, 3), (4, 3)])
        self.assertEqual(board.receive_shot(0, 0), {"result": "miss"})
        self.assertEqual(board.receive_shot(3, 3), {"result": "hit"})
        self.assertFalse(board.all_sunk())
        self.assertEqual(board.receive_shot(4, 3), {"result": "sunk", "ship": "Destroyer", "cells": [[3, 3], [4, 3]]})
        self.assertTrue(board.all_sunk())

    def test_random_layouts_are_legal(self):
        rng = random.Random(7)
        for _ in range(300):
            board = bs.Board()
            board.place_randomly(rng)
            self.assertEqual(len(board.owner), sum(n for _, n in bs.FLEET))
            self.assertTrue(all(bs.in_bounds(*c) for c in board.owner))
            for ship in board.ships:
                xs, ys = {x for x, _ in ship.cells}, {y for _, y in ship.cells}
                self.assertTrue(len(xs) == 1 or len(ys) == 1)

    def test_tracker_marks_whole_sunk_ship(self):
        tracker = bs.Tracker()
        tracker.record(4, 3, {"result": "sunk", "ship": "Destroyer", "cells": [[3, 3], [4, 3]]})
        self.assertEqual(tracker.state(3, 3), bs.SUNK)
        self.assertNotIn(("Destroyer", 2), tracker.remaining())


class BrainTests(unittest.TestCase):
    def test_finishes_games_without_repeats(self):
        rng = random.Random(3)
        totals = []
        for _ in range(40):
            board, brain = bs.Board(), bs.Brain(rng)
            board.place_randomly(rng)
            shots = set()
            while not board.all_sunk():
                x, y = brain.choose()
                self.assertNotIn((x, y), shots)
                shots.add((x, y))
                brain.view.record(x, y, board.receive_shot(x, y))
            totals.append(len(shots))
        self.assertLess(sum(totals) / len(totals), 62)  # random firing averages ~96


class PlacementTests(unittest.TestCase):
    def setUp(self):
        self.app = bs.App(FakeWin(lambda: -1), make_args(computer=True))

    def press(self, *keys):
        for key in keys:
            self.app.on_key(key)

    def test_moving_rotating_and_placing(self):
        app = self.app
        self.press(*[curses.KEY_RIGHT] * 9)
        self.assertEqual(app.cursor, (5, 0))  # a carrier can't hang off the edge
        self.press(ord("r"))
        self.assertFalse(app.horizontal)
        self.press(*[curses.KEY_DOWN] * 9)
        self.assertEqual(app.cursor, (5, 5))
        self.press(10)
        self.assertEqual(app.board.ships[0].cells, [(5, y) for y in range(5, 10)])
        self.press(ord("r"))  # battleship, horizontal, starting on the carrier
        self.assertEqual(app.cursor, (5, 5))
        self.press(10)
        self.assertEqual(len(app.board.ships), 1)
        self.assertEqual(app.flash[0], "That overlaps another ship.")
        self.press(8)
        self.assertEqual(app.board.ships, [])

    def test_random_then_start(self):
        self.press(ord(" "), 10)
        self.assertEqual(self.app.state, "ready")
        self.app.pump()  # the computer says it's ready
        self.assertEqual(self.app.state, "battle")


class ComputerGameTests(unittest.TestCase):
    def test_full_games_through_the_ui(self):
        bot = Bot(seed=5, rematches=2)
        play(make_args(computer=True, seed=11), bot)
        self.assertEqual(len(bot.results), 3)
        firsts = [first for _, _, first, _ in bot.results]
        self.assertEqual(firsts[0], not firsts[1])
        self.assertEqual(firsts[1], not firsts[2])
        for won, shots, _, screen in bot.results:
            self.assertTrue(17 <= shots <= 100)
            self.assertIn("You win!" if won else "You lose.", screen)

    def test_small_window_and_ascii(self):
        bot = Bot(seed=2)
        app = play(make_args(computer=True, ascii=True), bot, h=bs.MIN_H, w=bs.MIN_W)
        self.assertEqual(len(bot.results), 1)
        self.assertNotIn("·", bot.results[0][3])

    def test_too_small_window(self):
        win = FakeWin(lambda: ord("q"), h=10, w=40)
        bs.App(win, make_args()).run()
        self.assertIn("bigger", win.screen)


class NetworkTests(unittest.TestCase):
    def start_host(self, bot, name="Ann"):
        holder = {}

        def run():
            win = FakeWin(bot)
            app = bs.App(win, make_args(host=True, port=0, name=name, seed=21), ai_delay=0)
            holder["app"] = bot.app = app
            app.run()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        wait_for(lambda: "app" in holder and holder["app"].listener)
        return holder["app"], thread

    def test_two_players_with_a_rematch(self):
        host_bot, guest_bot = Bot(seed=1, rematches=1), Bot(seed=2, rematches=1)
        host, thread = self.start_host(host_bot)
        port = host.listener.port
        guest = play(make_args(join=f"127.0.0.1:{port}", name="Ben", seed=22), guest_bot)
        thread.join(10)
        self.assertEqual(len(host_bot.results), 2)
        self.assertEqual(len(guest_bot.results), 2)
        for (h_won, h_shots, h_first, _), (g_won, g_shots, g_first, _) in zip(host_bot.results, guest_bot.results):
            self.assertNotEqual(h_won, g_won)
            self.assertNotEqual(h_first, g_first)
        self.assertNotEqual(host_bot.results[0][2], host_bot.results[1][2])
        self.assertIn("Ann vs Ben", host_bot.results[0][3])
        self.assertIn("Ben vs Ann", guest_bot.results[0][3])

    def test_opponent_leaving_mid_game(self):
        host_bot, guest_bot = Bot(seed=1), Bot(seed=2, leave_after=3)
        host, thread = self.start_host(host_bot)
        play(make_args(join=f"127.0.0.1:{host.listener.port}", name="Ben"), guest_bot)
        thread.join(10)
        self.assertEqual(host_bot.notices, ["Ben left the game."])

    def raw_peer(self, hello):
        host_bot = Bot()
        host, thread = self.start_host(host_bot)
        sock = socket.create_connection(("127.0.0.1", host.listener.port))
        sock.sendall(hello)
        thread.join(10)
        sock.close()
        return host_bot.notices

    def test_old_version_is_refused(self):
        hello = {"type": "hello", "version": 1, "name": "Old", "board_size": 10, "ships": [5, 4, 3, 3, 2]}
        notices = self.raw_peer((json.dumps(hello) + "\n").encode())
        self.assertIn("different version", notices[0])

    def test_garbage_is_refused(self):
        notices = self.raw_peer(b"this is not json\n")
        self.assertIn("doesn't understand", notices[0])

    def test_join_nobody(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        bot = Bot()
        play(make_args(join=f"127.0.0.1:{port}"), bot)
        self.assertEqual(bot.notices, ["Nobody is hosting a game there."])

    def test_parse_address(self):
        self.assertEqual(bs.parse_address("10.0.0.5"), ("10.0.0.5", bs.DEFAULT_PORT))
        self.assertEqual(bs.parse_address(" pc.local:4000 "), ("pc.local", 4000))
        self.assertIsNone(bs.parse_address("10.0.0.5:99999"))
        self.assertIsNone(bs.parse_address(":4000"))


class ProtocolTests(unittest.TestCase):
    def test_out_of_turn_shot_is_rejected(self):
        board = bs.Board()
        board.place_randomly(random.Random(1))
        match = bs.Match(bs.Computer(random.Random(2), delay=99), board, my_turn=True)
        with self.assertRaises(bs.ProtocolError):
            match.handle({"type": "shot", "x": 1, "y": 1})
        with self.assertRaises(bs.ProtocolError):
            match.handle({"type": "result", "x": 1, "y": 1, "result": "hit"})


if __name__ == "__main__":
    unittest.main()
