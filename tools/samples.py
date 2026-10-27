import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from src.games.cards import banner_png

OUT = pathlib.Path(__file__).resolve().parent / "out"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "sample-hub.png"
    path.write_bytes(banner_png())
    print("wrote", path)


if __name__ == "__main__":
    main()
