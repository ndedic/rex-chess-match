#!/usr/bin/env python3
"""Claude vs DeepSeek chess, refereed through Rex.

  ./chess-match            build the Rex tab and start a game
  ./chess-match --swap     same, DeepSeek plays White

Layout built with the rex CLI:
  +------------------+-------------------+
  |  claude (White)  |                   |
  +------------------+   referee/board   |
  | opencode (Black) |                   |
  +------------------+-------------------+

The referee types prompts into each player's pane (`rex send`/`send-key`)
and reads replies back off the screen (`rex capture`). python-chess keeps
the real board and rejects illegal moves.
"""
import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time

import chess
import chess.pgn

HERE = os.path.dirname(os.path.abspath(__file__))
# Both players start here: an empty folder, away from the referee's code.
# players/opencode.json denies every opencode tool.
PLAYERS = os.path.join(HERE, "players")
# Claude Code with no built-in tools, no MCP servers, normal permission prompts.
CLAUDE_CMD = ["claude", "--tools", "", "--strict-mcp-config", "--permission-mode", "default"]
SESSION = os.environ.get("REX_SESSION", "")
MOVE_TIMEOUT = 240      # seconds a player gets per move
RETRIES = 3             # illegal/missing replies before forfeit
MAX_PLIES = 300         # adjudicate as a draw after this

# ---------------------------------------------------------------- rex helpers

def rex(*args, check=True):
    cmd = ["rex", *args]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)}\n{r.stderr.strip()}")
    return r.stdout


def rex_json(*args):
    return json.loads(rex(*args))


def send_line(block, text):
    rex("send", "-s", SESSION, "-b", block, text)
    time.sleep(0.8)
    rex("send-key", "-s", SESSION, "-b", block, "Enter")


def capture(block):
    return rex("capture", "-s", SESSION, "-b", block, "--trim", "--unwrap", check=False)

# ---------------------------------------------------------------- launcher

def launch(swap):
    if not SESSION:
        sys.exit("Run this inside Rex (REX_SESSION is not set).")
    w = rex_json("window", "new", "-s", SESSION, "Chess Match", "-c", PLAYERS,
                 "--json", "--", *CLAUDE_CMD)
    claude = w["block_ids"][0]
    board = rex_json("split", "-s", SESSION, "-b", claude, "--split=right",
                     "--ratio", "45", "-c", HERE, "--json")["block_ids"][0]
    deepseek = rex_json("split", "-s", SESSION, "-b", claude, "--split=below",
                        "-c", PLAYERS, "--json", "--", "opencode", PLAYERS)["block_ids"][0]

    # Start the referee visibly in the board pane.
    argv = [os.path.join(HERE, ".venv", "bin", "python"), os.path.join(HERE, "chess_match.py"),
            "--referee", "--claude", claude, "--deepseek", deepseek] + (["--swap"] if swap else [])
    cmd = " ".join(shlex.quote(a) for a in argv)
    time.sleep(1.5)
    send_line(board, cmd)
    print(f"Chess Match tab opened: {w.get('window_id', '')}")
    print(f"  claude   {claude}\n  deepseek {deepseek}\n  board    {board}")

# ---------------------------------------------------------------- rendering

LIGHT, DARK = "\033[48;5;180m", "\033[48;5;94m"
WFG, BFG = "\033[1;38;5;231m", "\033[1;38;5;16m"
RST, DIM, BOLD = "\033[0m", "\033[2m", "\033[1m"
GLYPH = {"k": "♚", "q": "♛", "r": "♜", "b": "♝", "n": "♞", "p": "♟"}
HILITE = "\033[48;5;143m"


def render(board, names, status, log):
    last = board.peek() if board.move_stack else None
    lit = {last.from_square, last.to_square} if last else set()
    out = ["\033[H\033[2J"]
    out.append(f"{BOLD}  ♔ {names[chess.WHITE]}  vs  ♚ {names[chess.BLACK]}{RST}\n\n")
    for rank in range(7, -1, -1):
        row = f"  {DIM}{rank + 1}{RST} "
        for file in range(8):
            sq = chess.square(file, rank)
            bg = HILITE if sq in lit else (LIGHT if (rank + file) % 2 else DARK)
            p = board.piece_at(sq)
            if p:
                fg = WFG if p.color == chess.WHITE else BFG
                row += f"{bg}{fg} {GLYPH[p.symbol().lower()]} {RST}"
            else:
                row += f"{bg}   {RST}"
        out.append(row + "\n")
    out.append(f"    {DIM} a  b  c  d  e  f  g  h{RST}\n\n")

    # Move list in SAN, numbered pairs.
    tmp, sans = chess.Board(), []
    for mv in board.move_stack:
        sans.append(tmp.san(mv))
        tmp.push(mv)
    pairs = [f"{i // 2 + 1}. {' '.join(sans[i:i + 2])}" for i in range(0, len(sans), 2)]
    out.append(f"  {BOLD}Moves{RST}\n")
    line = "  "
    for p in pairs[-24:]:
        if len(line) + len(p) > 44:
            out.append(line + "\n")
            line = "  "
        line += p + "  "
    out.append(line + "\n\n")
    out.append(f"  {BOLD}{status}{RST}\n\n")
    for entry in log[-8:]:
        out.append(f"  {DIM}{entry}{RST}\n")
    sys.stdout.write("".join(out))
    sys.stdout.flush()

# ---------------------------------------------------------------- referee

RULES = ("We're playing a chess match refereed by a script. Each turn I'll send "
         "the position as FEN plus the legal moves in UCI. Answer with ONLY your "
         "move, written as the letter M, then the ply number I give you, then '=', "
         "then the UCI move (on a retry the tag may look like 12r1). No tools, no commentary. Reply 'ok' now.")


def ask(block, board, tag, opp_move, note=""):
    side = "White" if board.turn == chess.WHITE else "Black"
    legal = " ".join(m.uci() for m in board.legal_moves)
    prev = f"Opponent played {opp_move}. " if opp_move else ""
    send_line(block, f"{note}{prev}You are {side}. Ply {tag}. FEN: {board.fen()} "
                     f"Legal: {legal}. Reply M{tag}= followed by your move.")


def wait_move(block, tag, tick):
    pat = re.compile(rf"M\s*{re.escape(tag)}\s*=\s*([a-h][1-8][a-h][1-8][qrbnQRBN]?)")
    start = time.time()
    while time.time() - start < MOVE_TIMEOUT:
        time.sleep(2)
        tick(int(time.time() - start))
        found = pat.findall(capture(block))
        if found:
            try:
                return chess.Move.from_uci(found[-1].lower()), None
            except ValueError:
                return None, found[-1]
    return None, "timeout"


def referee(args):
    blocks = {"Claude": args.claude, "DeepSeek": args.deepseek}
    white, black = ("DeepSeek", "Claude") if args.swap else ("Claude", "DeepSeek")
    names = {chess.WHITE: white, chess.BLACK: black}
    board, log = chess.Board(), []

    ready = {"Claude": re.compile(r"for shortcuts|shift\+tab to cycle"),
             "DeepSeek": re.compile(r"Ask anything|ctrl\+p commands")}
    for name, b in blocks.items():
        while True:
            screen = capture(b)
            footer = "\n".join(screen.strip().splitlines()[-4:])
            if name == "Claude" and re.search(r"bypass permissions|auto mode", footer):
                render(board, names, "Refusing to play: Claude is in an unattended "
                       "permission mode. Fix its settings and restart.", log)
                sys.exit(1)
            if "trust this folder" in screen:
                render(board, names, f"Answer the trust prompt in the {name} pane", log)
            elif ready[name].search(screen):
                break
            else:
                render(board, names, f"Waiting for {name} to start…", log)
            time.sleep(2)
    for name, b in blocks.items():
        send_line(b, RULES)
        log.append(f"briefed {name}")
    render(board, names, "Briefing players…", log)
    time.sleep(15)

    opp_move, result = None, None
    while not board.is_game_over() and len(board.move_stack) < MAX_PLIES:
        name = names[board.turn]
        ply = len(board.move_stack) + 1
        note, move = "", None
        for attempt in range(RETRIES):
            # A new tag per attempt, so an old rejected reply still on screen can't match.
            tag = str(ply) if attempt == 0 else f"{ply}r{attempt}"
            ask(blocks[name], board, tag, opp_move, note)
            t0 = time.time()
            move, bad = wait_move(
                blocks[name], tag,
                lambda s: render(board, names, f"{name} is thinking… {s}s", log))
            if move and move in board.legal_moves:
                break
            reason = bad if not move else f"illegal {move.uci()}"
            log.append(f"{name} ply {ply}: {reason}, retry {attempt + 1}")
            note = f"Your last answer was rejected ({reason}). "
            move = None
        if move is None:
            result = f"{name} forfeits (no legal move after {RETRIES} tries)"
            break
        san = board.san(move)
        board.push(move)
        opp_move = move.uci()
        log.append(f"{name}: {san} ({time.time() - t0:.0f}s)")
        render(board, names, f"{name} played {san}", log)

    if result is None:
        outcome = board.outcome()
        if outcome is None:
            result = "Draw by move limit"
        elif outcome.winner is None:
            result = f"Draw ({outcome.termination.name.lower().replace('_', ' ')})"
        else:
            result = f"{names[outcome.winner]} wins by {outcome.termination.name.lower()}"
    render(board, names, f"GAME OVER: {result}", log)

    os.makedirs(os.path.join(HERE, "games"), exist_ok=True)
    pgn_path = os.path.join(HERE, "games", f"game-{time.strftime('%Y%m%d-%H%M%S')}.pgn")
    game = chess.pgn.Game.from_board(board)
    game.headers.update(Event="Claude vs DeepSeek via Rex", Date=time.strftime("%Y.%m.%d"), White=white, Black=black, Result=board.result(claim_draw=True))
    with open(pgn_path, "w") as f:
        print(game, file=f)
    print(f"\n  PGN saved to {pgn_path}\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--referee", action="store_true")
    ap.add_argument("--claude")
    ap.add_argument("--deepseek")
    ap.add_argument("--swap", action="store_true", help="DeepSeek plays White")
    args = ap.parse_args()
    if args.referee:
        referee(args)
    else:
        launch(args.swap)


if __name__ == "__main__":
    main()
