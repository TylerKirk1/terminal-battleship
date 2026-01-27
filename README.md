# Terminal Battleship (LAN)

A minimal two-player Battleship game that runs in the terminal using curses and a simple TCP protocol. One player hosts, the other connects over the same network.

## Requirements
- Python 3.8+
- A terminal that supports curses (most macOS/Linux terminals do)

## Quick start

On the host machine:
```bash
python3 battleship.py --host --port 9999 --name Alice
```

On the other machine (same network):
```bash
python3 battleship.py --connect HOST_IP:9999 --name Bob
```

## Controls
- Arrow keys or WASD: move cursor
- Enter or Space: fire
- Q or Esc: quit

## Notes
- Ships are placed randomly to keep things simple.
- The game is turn-based; the host randomly chooses who goes first.
- If the terminal is too small, resize it and restart the game.
