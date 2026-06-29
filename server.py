from __future__ import annotations

import argparse
import json
import mimetypes
import random
import socket
import threading
import time
from collections import Counter, defaultdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parent
DEFAULT_PLAYERS = 2
ALLOWED_PLAYER_COUNTS = {2, 3}
STARTING_STACK = 1000
SMALL_BLIND = 5
BIG_BLIND = 10
FUN_CODE = "zhuyingjie"
RANKS = "23456789TJQKA"
SUITS = "SHDC"
RANK_VALUE = {rank: index + 2 for index, rank in enumerate(RANKS)}
SUIT_LABEL = {"S": "♠", "H": "♥", "D": "♦", "C": "♣"}
RANK_LABEL = {"T": "10", "J": "J", "Q": "Q", "K": "K", "A": "A", **{str(i): str(i) for i in range(2, 10)}}


rooms: dict[str, dict] = {}
rooms_lock = threading.RLock()
rooms_changed = threading.Condition(rooms_lock)


def normalize_room(room: str | None) -> str:
    room = (room or "room1").strip()
    room = "".join(ch for ch in room if ch.isalnum() or ch in "-_")
    return room[:40] or "room1"


def normalize_name(name: str | None) -> str:
    name = (name or "").strip()
    return name[:20] or "玩家"


def requested_player_count(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        count = int(value)
    except (TypeError, ValueError):
        return None
    return count if count in ALLOWED_PLAYER_COUNTS else None


def player_count(game: dict) -> int:
    return game["player_count"]


def new_room(room: str, count: int = DEFAULT_PLAYERS) -> dict:
    count = count if count in ALLOWED_PLAYER_COUNTS else DEFAULT_PLAYERS
    now = time.time()
    return {
        "room": room,
        "player_count": count,
        "players": [None for _ in range(count)],
        "spectators": {},
        "stacks": [STARTING_STACK for _ in range(count)],
        "dealer": 0,
        "small_blind": None,
        "big_blind": None,
        "deck": [],
        "hands": [[] for _ in range(count)],
        "board": [],
        "bets": [0 for _ in range(count)],
        "contributions": [0 for _ in range(count)],
        "folded": [False for _ in range(count)],
        "all_in": [False for _ in range(count)],
        "phase": "waiting",
        "turn": None,
        "current_bet": 0,
        "acted": set(),
        "winner_summary": [],
        "entertainment_mode": True,
        "message": f"等待 {count} 位玩家加入。",
        "hand_no": 0,
        "version": 0,
        "created_at": now,
        "updated_at": now,
    }


def get_room(room: str | None) -> dict:
    room = normalize_room(room)
    if room not in rooms:
        rooms[room] = new_room(room)
    return rooms[room]


def touch(game: dict) -> None:
    with rooms_changed:
        game["version"] += 1
        game["updated_at"] = time.time()
        rooms_changed.notify_all()


def occupied_seats(game: dict) -> list[int]:
    return [seat for seat, player in enumerate(game["players"]) if player is not None]


def table_full(game: dict) -> bool:
    return len(occupied_seats(game)) == player_count(game)


def seat_of(game: dict, player_id: str | None) -> int | None:
    if not player_id:
        return None
    for index, player in enumerate(game["players"]):
        if player and player["id"] == player_id:
            return index
    return None


def next_seat_after(game: dict, seat: int, *, require_can_act: bool = False, include_folded: bool = False) -> int | None:
    for step in range(1, player_count(game) + 1):
        candidate = (seat + step) % player_count(game)
        if game["players"][candidate] is None:
            continue
        if include_folded:
            return candidate
        if game["folded"][candidate]:
            continue
        if require_can_act and not can_act(game, candidate):
            continue
        return candidate
    return None


def can_act(game: dict, seat: int) -> bool:
    return bool(game["players"][seat]) and not game["folded"][seat] and not game["all_in"][seat]


def alive_seats(game: dict) -> list[int]:
    return [seat for seat in occupied_seats(game) if not game["folded"][seat]]


def action_seats(game: dict) -> list[int]:
    return [seat for seat in alive_seats(game) if not game["all_in"][seat]]


def public_player(player: dict | None) -> dict | None:
    return {"name": player["name"]} if player else None


def join_room(room: str | None, player_id: str | None, name: str | None, player_count_value: object = None) -> dict:
    if not player_id:
        raise ValueError("缺少 playerId")

    with rooms_lock:
        game = get_room(room)
        requested_count = requested_player_count(player_count_value)

        # A vacant waiting room can be configured by its first player. Once anyone
        # has a seat, its size is fixed so that a late visitor cannot disrupt a game.
        if (
            requested_count is not None
            and requested_count != player_count(game)
            and not occupied_seats(game)
            and not game["spectators"]
            and game["phase"] == "waiting"
        ):
            rooms[game["room"]] = new_room(game["room"], requested_count)
            game = rooms[game["room"]]

        name = normalize_name(name)
        seat = seat_of(game, player_id)
        if seat is not None:
            game["players"][seat]["name"] = name
        elif player_id in game["spectators"]:
            game["spectators"][player_id]["name"] = name
        else:
            player = {"id": player_id, "name": name}
            for index in range(player_count(game)):
                if game["players"][index] is None:
                    game["players"][index] = player
                    seat = index
                    break
            if seat is None:
                game["spectators"][player_id] = player

        if table_full(game) and game["phase"] == "waiting":
            start_hand(game, advance_dealer=False)
        elif game["phase"] == "waiting":
            game["message"] = f"等待 {player_count(game)} 位玩家加入，目前已有 {len(occupied_seats(game))} 位。"
            touch(game)
        return state_for(game, player_id)


def leave_room(room: str | None, player_id: str | None) -> dict:
    with rooms_lock:
        game = get_room(room)
        seat = seat_of(game, player_id)
        if seat is not None:
            game["players"][seat] = None
            game["phase"] = "waiting"
            game["turn"] = None
            game["message"] = f"有玩家离开，等待 {player_count(game)} 位玩家重新加入。"
        elif player_id in game["spectators"]:
            del game["spectators"][player_id]
        touch(game)
        return state_for(game, player_id)


def clear_room(room: str | None) -> dict:
    with rooms_lock:
        game = get_room(room)
        rooms[game["room"]] = new_room(game["room"], player_count(game))
        touch(rooms[game["room"]])
        return state_for(rooms[game["room"]], None)


def reset_table(room: str | None, player_id: str | None) -> dict:
    with rooms_lock:
        game = get_room(room)
        if seat_of(game, player_id) is None and table_full(game):
            raise PermissionError("只有牌桌上的玩家可以重置牌桌")

        players = game["players"]
        spectators = game["spectators"]
        dealer = game["dealer"]
        room_name = game["room"]
        count = player_count(game)
        rooms[room_name] = new_room(room_name, count)
        game = rooms[room_name]
        game["players"] = players
        game["spectators"] = spectators
        game["dealer"] = dealer
        if table_full(game):
            start_hand(game, advance_dealer=False)
        else:
            touch(game)
        return state_for(game, player_id)


def start_hand(game: dict, advance_dealer: bool = True) -> None:
    count = player_count(game)
    if not table_full(game):
        game["phase"] = "waiting"
        game["message"] = f"等待 {count} 位玩家加入，目前已有 {len(occupied_seats(game))} 位。"
        touch(game)
        return

    if any(stack <= 0 for stack in game["stacks"]):
        game["stacks"] = [STARTING_STACK for _ in range(count)]

    if advance_dealer:
        game["dealer"] = next_seat_after(game, game["dealer"], include_folded=True) or 0

    game["hand_no"] += 1
    game["deck"] = make_deck()
    random.shuffle(game["deck"])
    game["hands"] = [[draw(game), draw(game)] for _ in range(count)]
    game["board"] = []
    game["bets"] = [0 for _ in range(count)]
    game["contributions"] = [0 for _ in range(count)]
    game["folded"] = [False for _ in range(count)]
    game["all_in"] = [False for _ in range(count)]
    game["winner_summary"] = []
    game["acted"] = set()
    game["current_bet"] = 0
    game["phase"] = "preflop"

    if count == 2:
        # Heads-up special case: the button posts the small blind and acts first preflop.
        game["small_blind"] = game["dealer"]
        game["big_blind"] = next_seat_after(game, game["dealer"], include_folded=True)
    else:
        game["small_blind"] = next_seat_after(game, game["dealer"], include_folded=True)
        game["big_blind"] = next_seat_after(game, game["small_blind"], include_folded=True)
    post_bet(game, game["small_blind"], SMALL_BLIND)
    post_bet(game, game["big_blind"], BIG_BLIND)
    game["current_bet"] = max(game["bets"])
    game["turn"] = (
        game["small_blind"]
        if count == 2
        else next_seat_after(game, game["big_blind"], require_can_act=True)
    )
    game["message"] = f"第 {game['hand_no']} 手牌开始，{seat_label(game['dealer'])} 是按钮位。"
    touch(game)


def make_deck() -> list[str]:
    return [rank + suit for rank in RANKS for suit in SUITS]


def draw(game: dict) -> str:
    return game["deck"].pop()


def rebuild_deck_without_used(game: dict, scheduled_top: list[str] | None = None) -> None:
    scheduled_top = scheduled_top or []
    used = {card for hand in game["hands"] for card in hand}
    used.update(game["board"])
    used.update(scheduled_top)
    deck = [card for card in make_deck() if card not in used]
    random.shuffle(deck)
    # draw() pops from the end, so reverse the desired draw order when stacking.
    deck.extend(reversed(scheduled_top))
    game["deck"] = deck


def next_safe_card(used: set[str], reserved: set[str]) -> str:
    for card in make_deck():
        if card not in used and card not in reserved:
            used.add(card)
            return card
    raise RuntimeError("没有可用牌")


def apply_fun_code(room: str | None, player_id: str | None, code: str | None) -> dict:
    with rooms_lock:
        game = get_room(room)
        seat = seat_of(game, player_id)
        if seat is None:
            raise PermissionError("只有入座玩家可以使用娱乐码")
        if (code or "").strip().lower() != FUN_CODE:
            raise ValueError("娱乐码不正确")
        if game["phase"] not in {"preflop", "flop", "turn", "river"}:
            raise ValueError("本手开始后才能使用娱乐码")

        target_hand = ["AS", "KS"]
        target_board = ["QS", "JS", "TS"]
        reserved = set(target_hand + target_board)
        used: set[str] = set()

        # Seat using the code receives the nut hand pieces.
        game["hands"][seat] = target_hand[:]
        used.update(target_hand)

        # Keep other players' cards when possible, but remove cards needed by the entertainment hand.
        for other in range(player_count(game)):
            if other == seat:
                continue
            fixed = []
            for card in game["hands"][other]:
                if card in reserved or card in used:
                    fixed.append(None)
                else:
                    fixed.append(card)
                    used.add(card)
            while len(fixed) < 2:
                fixed.append(None)
            game["hands"][other] = fixed[:2]

        if len(game["board"]) >= 3:
            new_board = target_board[:]
            used.update(target_board)
            for old_card in game["board"][3:]:
                if old_card in reserved or old_card in used:
                    new_board.append(None)
                else:
                    new_board.append(old_card)
                    used.add(old_card)
            game["board"] = new_board[: len(game["board"])]
            scheduled_top: list[str] = []
        else:
            game["board"] = []
            scheduled_top = target_board[:]

        for other in range(player_count(game)):
            if other == seat:
                continue
            game["hands"][other] = [
                card if card is not None else next_safe_card(used, reserved)
                for card in game["hands"][other]
            ]

        game["board"] = [
            card if card is not None else next_safe_card(used, reserved)
            for card in game["board"]
        ]
        rebuild_deck_without_used(game, scheduled_top)
        game["message"] = "娱乐局进行中。"
        touch(game)
        return state_for(game, player_id)


def post_bet(game: dict, seat: int, amount: int) -> int:
    amount = max(0, min(int(amount), game["stacks"][seat]))
    game["stacks"][seat] -= amount
    game["bets"][seat] += amount
    game["contributions"][seat] += amount
    if game["stacks"][seat] == 0:
        game["all_in"][seat] = True
    return amount


def player_action(room: str | None, player_id: str | None, action: str, amount: int | None = None) -> dict:
    with rooms_lock:
        game = get_room(room)
        seat = seat_of(game, player_id)
        if seat is None:
            raise PermissionError("你是观战身份，不能行动")
        if game["phase"] not in {"preflop", "flop", "turn", "river"}:
            raise ValueError("当前不能行动")
        if game["turn"] != seat:
            raise ValueError("还没轮到你")
        if not can_act(game, seat):
            raise ValueError("你当前不能行动")

        action = (action or "").strip().lower()
        to_call = max(0, game["current_bet"] - game["bets"][seat])

        if action == "fold":
            game["folded"][seat] = True
            game["message"] = f"{seat_label(seat)}弃牌。"
            after_action(game, seat)
        elif action == "check_call":
            if to_call > 0:
                paid = post_bet(game, seat, to_call)
                game["message"] = f"{seat_label(seat)}跟注 {paid}。"
            else:
                game["message"] = f"{seat_label(seat)}过牌。"
            game["acted"].add(seat)
            after_action(game, seat)
        elif action == "raise":
            raise_by = int(amount or 0)
            total = to_call + raise_by
            if total <= 0:
                raise ValueError("加注金额不正确")
            paid_preview = min(total, game["stacks"][seat])
            new_bet_preview = game["bets"][seat] + paid_preview
            is_all_in_preview = paid_preview == game["stacks"][seat]
            if new_bet_preview <= game["current_bet"] and not is_all_in_preview:
                raise ValueError("加注后必须高于当前下注")
            if new_bet_preview > game["current_bet"] and raise_by < BIG_BLIND and not is_all_in_preview:
                raise ValueError(f"最小加注为 {BIG_BLIND}")

            paid = post_bet(game, seat, total)
            if game["bets"][seat] > game["current_bet"]:
                game["current_bet"] = game["bets"][seat]
                game["acted"] = {seat}
                game["message"] = f"{seat_label(seat)}加注到 {game['current_bet']}。"
            else:
                game["acted"].add(seat)
                game["message"] = f"{seat_label(seat)}全下跟注 {paid}。"
            after_action(game, seat)
        elif action == "all_in":
            if game["stacks"][seat] <= 0:
                raise ValueError("已经没有可下注筹码")
            paid = post_bet(game, seat, game["stacks"][seat])
            if paid <= 0:
                raise ValueError("全下失败")
            if game["bets"][seat] > game["current_bet"]:
                game["current_bet"] = game["bets"][seat]
                game["acted"] = {seat}
                game["message"] = f"{seat_label(seat)}全下到 {game['current_bet']}。"
            else:
                game["acted"].add(seat)
                game["message"] = f"{seat_label(seat)}全下跟注 {paid}。"
            after_action(game, seat)
        else:
            raise ValueError("未知动作")

        touch(game)
        return state_for(game, player_id)


def after_action(game: dict, seat: int) -> None:
    alive = alive_seats(game)
    if len(alive) == 1:
        finish_by_fold(game, alive[0])
        return
    if betting_round_complete(game):
        complete_street(game)
        return
    next_seat = next_seat_after(game, seat, require_can_act=True)
    if next_seat is None:
        complete_street(game)
    else:
        game["turn"] = next_seat


def betting_round_complete(game: dict) -> bool:
    alive = alive_seats(game)
    if len(alive) <= 1:
        return True
    for seat in action_seats(game):
        if seat not in game["acted"]:
            return False
        if game["bets"][seat] != game["current_bet"]:
            return False
    return True


def complete_street(game: dict) -> None:
    count = player_count(game)
    game["bets"] = [0 for _ in range(count)]
    game["current_bet"] = 0
    game["acted"] = set()

    if len(alive_seats(game)) == 1:
        finish_by_fold(game, alive_seats(game)[0])
        return
    if len(action_seats(game)) <= 1:
        while len(game["board"]) < 5:
            deal_next_board_card(game)
        showdown(game)
        return

    if game["phase"] == "preflop":
        game["board"].extend([draw(game), draw(game), draw(game)])
        game["phase"] = "flop"
        begin_postflop_action(game)
    elif game["phase"] == "flop":
        deal_next_board_card(game)
        game["phase"] = "turn"
        begin_postflop_action(game)
    elif game["phase"] == "turn":
        deal_next_board_card(game)
        game["phase"] = "river"
        begin_postflop_action(game)
    elif game["phase"] == "river":
        showdown(game)


def deal_next_board_card(game: dict) -> None:
    game["board"].append(draw(game))


def begin_postflop_action(game: dict) -> None:
    first = next_seat_after(game, game["dealer"], require_can_act=True)
    game["turn"] = first
    if first is None:
        complete_street(game)
    else:
        game["message"] = f"{phase_label(game['phase'])}，轮到 {seat_label(first)} 行动。"


def finish_by_fold(game: dict, winner: int) -> None:
    won = sum(game["contributions"])
    game["stacks"][winner] += won
    game["bets"] = [0 for _ in range(player_count(game))]
    game["contributions"] = [0 for _ in range(player_count(game))]
    game["phase"] = "hand_over"
    game["turn"] = None
    game["winner_summary"] = [{"seat": winner, "name": game["players"][winner]["name"], "amount": won, "handName": "其他玩家弃牌"}]
    game["message"] = f"{seat_label(winner)}赢得底池 {won}。"


def side_pots(contributions: list[int], folded: list[bool]) -> list[dict]:
    pots = []
    levels = sorted({amount for amount in contributions if amount > 0})
    previous = 0
    for level in levels:
        amount = sum(max(0, min(contribution, level) - previous) for contribution in contributions)
        eligible = [seat for seat, contribution in enumerate(contributions) if contribution >= level and not folded[seat]]
        if amount > 0 and eligible:
            pots.append({"amount": amount, "eligible": eligible})
        previous = level
    return pots


def showdown(game: dict) -> None:
    while len(game["board"]) < 5:
        deal_next_board_card(game)

    scores: dict[int, tuple[int, list[int]]] = {}
    hand_names: dict[int, str] = {}
    for seat in alive_seats(game):
        score, hand_name = evaluate_hand(game["hands"][seat] + game["board"])
        scores[seat] = score
        hand_names[seat] = hand_name

    awards: dict[int, int] = defaultdict(int)
    award_names: dict[int, str] = {}
    for pot in side_pots(game["contributions"], game["folded"]):
        eligible = [seat for seat in pot["eligible"] if seat in scores]
        if not eligible:
            continue
        best = max(scores[seat] for seat in eligible)
        winners = [seat for seat in eligible if scores[seat] == best]
        share, remainder = divmod(pot["amount"], len(winners))
        for index, winner in enumerate(sorted(winners)):
            awards[winner] += share + (1 if index < remainder else 0)
            award_names[winner] = hand_names[winner]

    summaries = []
    for seat in sorted(awards):
        amount = awards[seat]
        game["stacks"][seat] += amount
        summaries.append({"seat": seat, "name": game["players"][seat]["name"], "amount": amount, "handName": award_names.get(seat, "获胜牌型")})

    game["bets"] = [0 for _ in range(player_count(game))]
    game["contributions"] = [0 for _ in range(player_count(game))]
    game["phase"] = "hand_over"
    game["turn"] = None
    game["winner_summary"] = summaries
    if len(summaries) == 1:
        item = summaries[0]
        game["message"] = f"{seat_label(item['seat'])}以「{item['handName']}」赢得 {item['amount']}。"
    else:
        game["message"] = "分池结算：" + "；".join(f"{seat_label(item['seat'])} {item['amount']}（{item['handName']}）" for item in summaries)


def next_hand(room: str | None, player_id: str | None) -> dict:
    with rooms_lock:
        game = get_room(room)
        if seat_of(game, player_id) is None:
            raise PermissionError("只有牌桌上的玩家可以开始下一手")
        start_hand(game, advance_dealer=True)
        return state_for(game, player_id)


def state_for(game: dict, player_id: str | None) -> dict:
    seat = seat_of(game, player_id)
    role = f"seat{seat}" if seat is not None else ("spectator" if player_id in game["spectators"] else None)
    reveal_all = game["phase"] == "hand_over"

    hands = []
    for index in range(player_count(game)):
        if not game["players"][index]:
            hands.append([])
        elif reveal_all or index == seat:
            hands.append([card_public(card) for card in game["hands"][index]])
        else:
            hands.append([None, None])

    to_call = 0
    legal = {}
    if seat is not None and game["turn"] == seat and game["phase"] in {"preflop", "flop", "turn", "river"}:
        to_call = max(0, game["current_bet"] - game["bets"][seat])
        legal = {
            "canFold": True,
            "canCheckCall": True,
            "checkCallLabel": "跟注" if to_call > 0 else "过牌",
            "toCall": to_call,
            "minRaise": BIG_BLIND,
            "stack": game["stacks"][seat],
            "canRaise": game["stacks"][seat] > to_call,
            "canAllIn": game["stacks"][seat] > 0,
        }

    return {
        "room": game["room"],
        "seatCount": player_count(game),
        "phase": game["phase"],
        "phaseLabel": phase_label(game["phase"]),
        "players": [public_player(player) for player in game["players"]],
        "spectatorCount": len(game["spectators"]),
        "you": role,
        "yourSeat": seat,
        "dealer": game["dealer"],
        "smallBlind": game["small_blind"],
        "bigBlind": game["big_blind"],
        "turn": game["turn"],
        "handNo": game["hand_no"],
        "stacks": game["stacks"],
        "bets": game["bets"],
        "contributions": game["contributions"],
        "pot": sum(game["contributions"]) - sum(game["bets"]),
        "totalPot": sum(game["contributions"]),
        "currentBet": game["current_bet"],
        "board": [card_public(card) for card in game["board"]],
        "hands": hands,
        "folded": game["folded"],
        "allIn": game["all_in"],
        "winnerSummary": game["winner_summary"],
        "message": game["message"],
        "legal": legal,
        "entertainmentMode": True,
        "version": game["version"],
        "started": table_full(game),
        "rules": {"startingStack": STARTING_STACK, "smallBlind": SMALL_BLIND, "bigBlind": BIG_BLIND},
    }


def card_public(card: str) -> dict:
    rank, suit = card[0], card[1]
    return {"code": card, "rank": RANK_LABEL[rank], "suit": SUIT_LABEL[suit], "color": "red" if suit in {"H", "D"} else "black"}


def phase_label(phase: str) -> str:
    return {"waiting": "等待入座", "preflop": "翻牌前", "flop": "翻牌圈", "turn": "转牌圈", "river": "河牌圈", "hand_over": "本手结束"}.get(phase, phase)


def seat_label(seat: int | None) -> str:
    return "玩家" if seat is None else f"玩家{seat + 1}"


def straight_high(values: list[int]) -> int | None:
    unique = sorted(set(values), reverse=True)
    if 14 in unique:
        unique.append(1)
    for index in range(len(unique) - 4):
        window = unique[index:index + 5]
        if window[0] - window[4] == 4 and len(window) == 5:
            return window[0]
    return None


def evaluate_hand(cards: list[str]) -> tuple[tuple[int, list[int]], str]:
    values = [RANK_VALUE[card[0]] for card in cards]
    counts = Counter(values)
    by_count: dict[int, list[int]] = defaultdict(list)
    for value, count in counts.items():
        by_count[count].append(value)
    for count in by_count:
        by_count[count].sort(reverse=True)

    suits: dict[str, list[int]] = defaultdict(list)
    for card in cards:
        suits[card[1]].append(RANK_VALUE[card[0]])
    for suit_values in suits.values():
        if len(suit_values) >= 5:
            high = straight_high(suit_values)
            if high:
                return (8, [high]), "同花顺"

    if by_count[4]:
        four = by_count[4][0]
        return (7, [four, max(value for value in values if value != four)]), "四条"

    trips = sorted([value for value, count in counts.items() if count >= 3], reverse=True)
    pairs = sorted([value for value, count in counts.items() if count >= 2], reverse=True)
    if trips:
        trip = trips[0]
        pair_candidates = [value for value in pairs if value != trip] + trips[1:]
        if pair_candidates:
            return (6, [trip, max(pair_candidates)]), "葫芦"

    flush_values = None
    for suit_values in suits.values():
        if len(suit_values) >= 5:
            top = sorted(suit_values, reverse=True)[:5]
            if flush_values is None or top > flush_values:
                flush_values = top
    if flush_values:
        return (5, flush_values), "同花"

    high = straight_high(values)
    if high:
        return (4, [high]), "顺子"
    if trips:
        trip = trips[0]
        return (3, [trip] + sorted([value for value in values if value != trip], reverse=True)[:2]), "三条"
    if len(pairs) >= 2:
        top_two = pairs[:2]
        return (2, top_two + [max(value for value in values if value not in top_two)]), "两对"
    if len(pairs) == 1:
        pair = pairs[0]
        return (1, [pair] + sorted([value for value in values if value != pair], reverse=True)[:3]), "一对"
    return (0, sorted(values, reverse=True)[:5]), "高牌"


class PokerHandler(BaseHTTPRequestHandler):
    server_version = "PokerRoom/2.0"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/state":
            qs = parse_qs(parsed.query)
            with rooms_lock:
                game = get_room(qs.get("room", ["room1"])[0])
                self.send_json(state_for(game, qs.get("playerId", [""])[0]))
            return
        if parsed.path == "/api/events":
            qs = parse_qs(parsed.query)
            self.send_events(qs.get("room", ["room1"])[0], qs.get("playerId", [""])[0])
            return
        self.serve_static(parsed.path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            payload = self.read_json()
            if parsed.path == "/api/join":
                self.send_json(join_room(payload.get("room"), payload.get("playerId"), payload.get("name"), payload.get("playerCount")))
            elif parsed.path == "/api/leave":
                self.send_json(leave_room(payload.get("room"), payload.get("playerId")))
            elif parsed.path == "/api/action":
                self.send_json(player_action(payload.get("room"), payload.get("playerId"), payload.get("action"), payload.get("amount")))
            elif parsed.path == "/api/fun-code":
                self.send_json(apply_fun_code(payload.get("room"), payload.get("playerId"), payload.get("code")))
            elif parsed.path == "/api/next-hand":
                self.send_json(next_hand(payload.get("room"), payload.get("playerId")))
            elif parsed.path == "/api/reset-table":
                self.send_json(reset_table(payload.get("room"), payload.get("playerId")))
            elif parsed.path == "/api/clear-room":
                self.send_json(clear_room(payload.get("room")))
            else:
                self.send_error_json(HTTPStatus.NOT_FOUND, "接口不存在")
        except PermissionError as exc:
            self.send_error_json(HTTPStatus.FORBIDDEN, str(exc))
        except Exception as exc:
            self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        data = self.rfile.read(length).decode("utf-8") if length else "{}"
        return json.loads(data)

    def send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_error_json(self, status: HTTPStatus, message: str) -> None:
        self.send_json({"ok": False, "error": message}, status)

    def send_events(self, room: str | None, player_id: str | None) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        room = normalize_room(room)
        player_id = player_id or ""
        last_version = -1
        deadline = time.time() + 120
        try:
            while time.time() < deadline:
                payload = None
                with rooms_changed:
                    game = get_room(room)
                    version = game["version"]
                    if version <= last_version:
                        rooms_changed.wait(timeout=20)
                        game = get_room(room)
                        version = game["version"]
                    if version > last_version:
                        payload = state_for(game, player_id)
                        last_version = version

                if payload is None:
                    chunk = b": ping\n\n"
                else:
                    data = json.dumps(payload, ensure_ascii=False)
                    chunk = f"id: {last_version}\nevent: state\ndata: {data}\n\n".encode("utf-8")
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, TimeoutError):
            return

    def serve_static(self, request_path: str) -> None:
        if request_path in ("", "/"):
            request_path = "/index.html"
        target = (ROOT / request_path.lstrip("/")).resolve()
        if not str(target).startswith(str(ROOT)) or not target.exists() or target.is_dir():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content = target.read_bytes()
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if content_type.startswith("text/"):
            content_type += "; charset=utf-8"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, fmt: str, *args) -> None:
        print("[%s] %s" % (self.log_date_time_string(), fmt % args))


def lan_addresses() -> list[str]:
    addresses = {"127.0.0.1"}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addresses.add(info[4][0])
    except socket.gaierror:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            addresses.add(sock.getsockname()[0])
    except OSError:
        pass
    return sorted(addresses)


def run(host: str, port: int) -> None:
    server = ThreadingHTTPServer((host, port), PokerHandler)
    print("二人 / 三人德州扑克服务器已启动")
    print(f"本机打开：http://127.0.0.1:{port}")
    print("同一局域网另一台电脑可尝试：")
    for address in lan_addresses():
        if not address.startswith("127."):
            print(f"  http://{address}:{port}")
    print("按 Ctrl+C 停止服务器。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务器已停止。")
    finally:
        server.server_close()


def play_checks_to_end(room: str, game: dict) -> dict:
    for _ in range(30):
        if game["phase"] == "hand_over":
            return state_for(game, game["players"][0]["id"])
        turn = game["turn"]
        assert turn is not None
        state = player_action(room, game["players"][turn]["id"], "check_call")
    raise AssertionError("hand did not finish")


def self_test() -> None:
    for count in (2, 3):
        rooms.clear()
        room = f"test{count}"
        for index in range(count):
            state = join_room(room, f"p{index}", f"玩家{index + 1}", count)
        game = get_room(room)
        assert state["phase"] == "preflop"
        assert state["seatCount"] == count
        assert state["dealer"] == 0
        assert state["smallBlind"] == (0 if count == 2 else 1)
        assert state["bigBlind"] == (1 if count == 2 else 2)
        assert state["turn"] == 0
        assert sum(state["stacks"]) + state["totalPot"] == STARTING_STACK * count

        for _ in range(count):
            turn = game["turn"]
            assert turn is not None
            state = player_action(room, game["players"][turn]["id"], "check_call")
        assert state["phase"] == "flop"
        assert len(state["board"]) == 3
        assert state["turn"] == (1 if count == 3 else 1)
        state = play_checks_to_end(room, game)
        assert state["phase"] == "hand_over"
        assert sum(state["stacks"]) + state["totalPot"] == STARTING_STACK * count

    assert evaluate_hand(["AS", "KS", "QS", "JS", "TS", "2D", "3C"])[1] == "同花顺"
    assert evaluate_hand(["AS", "AH", "AD", "AC", "2S", "3D", "4C"])[1] == "四条"
    assert evaluate_hand(["AS", "AH", "AD", "KC", "KS", "3D", "4C"])[1] == "葫芦"

    assert side_pots([50, 100, 200], [False, False, False]) == [
        {"amount": 150, "eligible": [0, 1, 2]},
        {"amount": 100, "eligible": [1, 2]},
        {"amount": 100, "eligible": [2]},
    ]
    print("self-test passed")


def main() -> None:
    parser = argparse.ArgumentParser(description="二人 / 三人德州扑克网页服务器")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8010)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
    else:
        run(args.host, args.port)


if __name__ == "__main__":
    main()
