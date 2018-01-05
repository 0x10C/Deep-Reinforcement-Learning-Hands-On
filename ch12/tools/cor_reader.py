#!/usr/bin/env python3
import os
import sys
import argparse
import collections
sys.path.append(os.getcwd())

from libbots import cornell


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("-g", "--genre", help="Genre to show dialogs from")
    args = parser.parse_args()

    genre_counts = collections.Counter()
    genres = cornell.read_genres(cornell.DATA_DIR)
    for movie, g_list in genres.items():
        for g in g_list:
            genre_counts[g] += 1
    print("Genres:")
    for g, count in genre_counts.most_common():
        print("%s: %d" % (g, count))

    if args.genre is not None:
        dials = cornell.load_dialogues(genre_filter=args.genre)
        for d_idx, dial in enumerate(dials):
            print("Dialog %d with %d phrases:" % (d_idx, len(dial)))
            for p in dial:
                print(" ".join(p.words))
            print()
    pass
