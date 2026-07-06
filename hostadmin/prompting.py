"""Interactive prompt helpers shared by the host-admin wizards."""

from __future__ import annotations


def prompt(label: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default is not None else ""
    while True:
        ans = input(f"{label}{suffix}: ").strip()
        if ans:
            return ans
        if default is not None:
            return default


def prompt_choice(label: str, choices: list[str], default: str) -> str:
    print(label)
    for i, c in enumerate(choices, 1):
        marker = " (default)" if c == default else ""
        print(f"  {i}. {c}{marker}")
    while True:
        ans = input(f"Pick 1-{len(choices)} [{default}]: ").strip()
        if not ans:
            return default
        if ans.isdigit() and 1 <= int(ans) <= len(choices):
            return choices[int(ans) - 1]
        if ans in choices:
            return ans
        print("  invalid choice; try again")


def prompt_int(label: str, default: int, minimum: int = 0) -> int:
    while True:
        ans = input(f"{label} [{default}]: ").strip()
        if not ans:
            return default
        try:
            v = int(ans)
        except ValueError:
            print("  expected integer")
            continue
        if v < minimum:
            print(f"  must be >= {minimum}")
            continue
        return v
