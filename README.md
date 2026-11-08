# Terminal Battleship

Battleship in your terminal. Play the computer, or a friend on the same network.

## Run it

Needs Python 3.8+. On Windows, install curses first:

```
python -m pip install -r requirements.txt
```

Then:

```
python battleship.py
```

Pick a mode from the menu. You can also skip it with `--computer`, `--host`, or `--join 192.168.1.20` (add `:PORT` if the host used `--port`). `--name` sets your name; `--ascii` swaps the symbols for plain ASCII if your terminal shows boxes.

## Playing

Place your five ships (arrows move, R rotates, Enter places, Space places them all randomly), then take turns firing. The first to sink the whole fleet wins. After a game, press N for a rematch.

When hosting, the screen shows the address your friend should type. Windows may ask to let Python through the firewall the first time.

Tests: `python -m unittest`
