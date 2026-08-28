#!/usr/bin/env python3
"""
clean_goodreads.py
------------------
Normalises the "Goodreads Book Datasets 10M" Kaggle dump into two clean,
BigQuery-ready tables.

Handles the following known defects in the raw data:

  1.  Six different header variants across the 23 book*.csv chunks
  2.  Randomly scrambled column ORDER in the early chunks (1-100k .. 400k-500k)
  3.  'pagesNumber' vs 'PagesNumber' case inconsistency
  4.  'Count of text reviews' contains spaces -> invalid BigQuery column name
  5.  RatingDist1-5 / RatingDistTotal stored as prefixed strings ("5:1546466")
  6.  PublishMonth / PublishDay are SWAPPED in the raw data
  7.  ISBN must stay a STRING (leading zeros are significant)
  8.  ISBN is missing on many rows
  9.  'Description' column absent from 6 of the 23 chunks
 10.  user_rating files store ratings as text labels ("it was amazing")
 11.  user_rating files have no book Id -- only a title string

Usage
-----
    python3 clean_goodreads.py --input ~/Downloads/goodreads-data --output ./clean

Outputs
-------
    clean/books_clean.csv          one row per book, canonical schema
    clean/user_ratings_clean.csv   one row per rating, numeric scale
    clean/books_schema.json        BigQuery schema (bq load --schema)
    clean/user_ratings_schema.json BigQuery schema
    clean/cleaning_report.txt      what was changed and why
"""

import argparse
import glob
import json
import os
import re
import sys
from collections import Counter

import pandas as pd

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

# Canonical output column order for the books table.
BOOK_COLUMNS = [
    "id",
    "name",
    "authors",
    "isbn",
    "rating",
    "publish_year",
    "publish_month",
    "publish_day",
    "raw_publish_month",
    "raw_publish_day",
    "publisher",
    "language",
    "language_normalised",
    "pages_number",
    "rating_dist_1",
    "rating_dist_2",
    "rating_dist_3",
    "rating_dist_4",
    "rating_dist_5",
    "rating_dist_total",
    "counts_of_review",
    "count_of_text_reviews",
    "description",
    "source_file",
]

# Maps every lowercased raw header we might encounter -> canonical name.
# Lowercasing first is what collapses pagesNumber / PagesNumber together.
HEADER_MAP = {
    "id": "id",
    "name": "name",
    "authors": "authors",
    "isbn": "isbn",
    "rating": "rating",
    "publishyear": "publish_year",
    # Mapped straight through. The month/day swap is NOT universal across
    # the chunk files, so it is resolved per row later in resolve_dates().
    "publishmonth": "raw_publish_month",
    "publishday": "raw_publish_day",
    "publisher": "publisher",
    "language": "language",
    "pagesnumber": "pages_number",
    "ratingdist1": "rating_dist_1",
    "ratingdist2": "rating_dist_2",
    "ratingdist3": "rating_dist_3",
    "ratingdist4": "rating_dist_4",
    "ratingdist5": "rating_dist_5",
    "ratingdisttotal": "rating_dist_total",
    "countsofreview": "counts_of_review",
    "count of text reviews": "count_of_text_reviews",
    "description": "description",
}

DIST_COLUMNS = [
    "rating_dist_1",
    "rating_dist_2",
    "rating_dist_3",
    "rating_dist_4",
    "rating_dist_5",
    "rating_dist_total",
]

# Goodreads text labels -> numeric scale.
RATING_LABEL_MAP = {
    "it was amazing": 5,
    "really liked it": 4,
    "liked it": 3,
    "it was ok": 2,
    "it was okay": 2,
    "did not like it": 1,
}

# Rows whose Rating matches any of these are dropped (placeholders, not ratings).
RATING_DROP_PATTERNS = [
    "doesn't have any rating",
    "does not have any rating",
    "no rating",
]

# BigQuery types for the books table.
BOOKS_BQ_SCHEMA = [
    ("id", "INTEGER"),
    ("name", "STRING"),
    ("authors", "STRING"),
    ("isbn", "STRING"),
    ("rating", "FLOAT"),
    ("publish_year", "INTEGER"),
    ("publish_month", "INTEGER"),
    ("publish_day", "INTEGER"),
    ("publisher", "STRING"),
    ("language", "STRING"),
    ("language_normalised", "STRING"),
    ("pages_number", "INTEGER"),
    ("rating_dist_1", "INTEGER"),
    ("rating_dist_2", "INTEGER"),
    ("rating_dist_3", "INTEGER"),
    ("rating_dist_4", "INTEGER"),
    ("rating_dist_5", "INTEGER"),
    ("rating_dist_total", "INTEGER"),
    ("counts_of_review", "INTEGER"),
    ("count_of_text_reviews", "INTEGER"),
    ("description", "STRING"),
    ("source_file", "STRING"),
]

USER_RATINGS_BQ_SCHEMA = [
    ("user_id", "INTEGER"),
    ("book_title", "STRING"),
    ("book_title_normalised", "STRING"),
    ("rating_label", "STRING"),
    ("rating", "INTEGER"),
]


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def read_csv_resilient(path: str) -> pd.DataFrame:
    """
    Read a CSV as all-strings. Uses the fast C parser first (important:
    the raw dump is 1.2 GB and the pure-Python parser is an order of
    magnitude slower), falling back to the Python parser only if the C
    one chokes on a malformed line.

    dtype=str is deliberate: it protects ISBN leading zeros and stops
    pandas type-guessing differently on each of the six schema variants.
    """
    kwargs = dict(
        dtype=str,
        keep_default_na=False,
        na_values=[""],
        on_bad_lines="skip",
        encoding="utf-8",
        encoding_errors="replace",
    )
    try:
        return pd.read_csv(path, engine="c", **kwargs)
    except Exception:
        return pd.read_csv(path, engine="python", **kwargs)


def strip_dist_prefix(series: pd.Series) -> pd.Series:
    """'5:1546466' -> 1546466 ; 'total:2298124' -> 2298124 ; '' -> NA."""
    s = series.astype("string")
    # Take everything after the last colon. If no colon, take the whole value.
    s = s.str.rsplit(":", n=1).str[-1]
    s = s.str.strip()
    # Anything that isn't a run of digits becomes NA.
    s = s.where(s.str.fullmatch(r"\d+", na=False))
    return pd.to_numeric(s, errors="coerce").astype("Int64")


def normalise_title(series: pd.Series) -> pd.Series:
    """
    Lowercase, strip a trailing series suffix like ' (Harry Potter, #6)',
    collapse whitespace and drop punctuation. Used to make the user_rating
    titles at least *approximately* joinable to book names.
    """
    s = series.astype("string").str.lower()
    s = s.str.replace(r"\s*\([^()]*#\s*\d+[^()]*\)\s*$", "", regex=True)
    s = s.str.replace(r"[^\w\s]", " ", regex=True)
    s = s.str.replace(r"\s+", " ", regex=True).str.strip()
    return s


def clean_isbn(series: pd.Series) -> pd.Series:
    """Keep as string, preserve leading zeros, blank -> NA."""
    s = series.astype("string").str.strip()
    s = s.str.replace(r"[^0-9Xx]", "", regex=True)
    s = s.str.upper()
    s = s.where(s.str.len().isin([10, 13]))
    return s


def normalise_language(series: pd.Series) -> pd.Series:
    """
    'eng', 'en-US', 'en-GB', 'en_CA' all mean English. Collapse any
    regional variant down to its base ISO code so GROUP BY language
    doesn't fragment. Adds a separate normalised column; the raw value
    is kept in `language`.
    """
    s = series.astype("string").str.strip().str.lower()
    s = s.str.replace("_", "-", regex=False)
    base = s.str.split("-").str[0]
    # Collapse the common 3-letter forms onto their 2-letter equivalents.
    three_to_two = {
        "eng": "en", "spa": "es", "fre": "fr", "fra": "fr", "ger": "de",
        "deu": "de", "ita": "it", "por": "pt", "dut": "nl", "nld": "nl",
        "rus": "ru", "jpn": "ja", "chi": "zh", "zho": "zh", "ara": "ar",
        "kor": "ko", "pol": "pl", "swe": "sv", "tur": "tr", "heb": "he",
        "urd": "ur", "hin": "hi", "ben": "bn", "per": "fa", "fas": "fa",
        "gre": "el", "ell": "el", "cze": "cs", "ces": "cs", "dan": "da",
        "fin": "fi", "nor": "no", "hun": "hu", "rum": "ro", "ron": "ro",
        "ukr": "uk", "vie": "vi", "tha": "th", "ind": "id", "cat": "ca",
    }
    base = base.replace(three_to_two)
    return base.where(base.str.len().isin([2, 3]))


def to_int(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").astype("Int64")


def to_float(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce")


def write_bq_schema(path: str, schema) -> None:
    payload = [
        {"name": n, "type": t, "mode": "NULLABLE"} for n, t in schema
    ]
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)


# --------------------------------------------------------------------------
# Books
# --------------------------------------------------------------------------

def load_book_file(path: str, report: list) -> pd.DataFrame:
    """Read one book*.csv chunk and map it onto the canonical schema."""
    fname = os.path.basename(path)

    # dtype=str on read is what protects ISBN's leading zeros and stops
    # pandas guessing wrong on the mixed-type columns. We cast deliberately
    # afterwards. Bad lines are skipped rather than killing the run.
    df = read_csv_resilient(path)

    raw_cols = list(df.columns)

    # Lowercase + trim every header, then map to canonical names.
    lowered = {c: c.strip().lower() for c in raw_cols}
    df = df.rename(columns=lowered)

    unmapped = [c for c in df.columns if c not in HEADER_MAP]
    if unmapped:
        report.append(f"  {fname}: dropped unrecognised column(s): {unmapped}")
        df = df.drop(columns=unmapped)

    df = df.rename(columns=HEADER_MAP)

    # Any canonical column this chunk simply doesn't have becomes NA.
    derived = {"source_file", "language_normalised", "publish_month", "publish_day"}
    missing = [c for c in BOOK_COLUMNS if c not in df.columns and c not in derived]
    for col in missing:
        df[col] = pd.NA
    if missing:
        report.append(f"  {fname}: absent column(s) filled with NULL: {missing}")

    df["source_file"] = fname

    # Reindex forces the canonical ORDER regardless of how scrambled the
    # source file was. This is the step that defeats problem #2.
    df = df.reindex(columns=BOOK_COLUMNS)
    return df


def resolve_dates(df, mode, report):
    """
    Decide which raw column is really the month.

    A month can never exceed 12, so for each row we inspect the pair:
      - one <=12, other >12  -> the >12 value must be the day
      - both <=12            -> ambiguous; fall back to the per-file
                                majority verdict from unambiguous rows
      - out of 1..31         -> junk, left null

    mode: perrow (default, safest) | swap (force) | asis (trust labels)
    """
    rm = pd.to_numeric(df["raw_publish_month"], errors="coerce").astype("Int64")
    rd = pd.to_numeric(df["raw_publish_day"], errors="coerce").astype("Int64")

    if mode == "asis":
        df["publish_month"], df["publish_day"] = rm, rd
        report.append("  date mode: asis (labels trusted)")
    elif mode == "swap":
        df["publish_month"], df["publish_day"] = rd, rm
        report.append("  date mode: swap (raw month treated as day)")
    else:
        month = pd.Series(pd.NA, index=df.index, dtype="Int64")
        day = pd.Series(pd.NA, index=df.index, dtype="Int64")

        both = rm.notna() & rd.notna()

        # Case A: raw month > 12, raw day <= 12  -> this row IS swapped
        a = both & (rm > 12) & (rd <= 12)
        month[a], day[a] = rd[a], rm[a]

        # Case B: raw day > 12, raw month <= 12  -> this row is correct
        b = both & (rd > 12) & (rm <= 12)
        month[b], day[b] = rm[b], rd[b]

        # Case C: both <= 12 -> ambiguous, decide by file majority
        c = both & (rm <= 12) & (rd <= 12)
        if bool(c.any()):
            votes = (
                pd.DataFrame({"f": df["source_file"], "a": a, "b": b})
                .groupby("f")[["a", "b"]].sum()
            )
            swap_files = set(votes.index[votes["a"] >= votes["b"]])
            is_swap = df["source_file"].isin(swap_files)
            cs = c & is_swap
            cn = c & ~is_swap
            month[cs], day[cs] = rd[cs], rm[cs]
            month[cn], day[cn] = rm[cn], rd[cn]
            report.append(
                "  date mode: perrow | swapped rows: {:,} | correct rows: {:,} | "
                "ambiguous resolved by file majority: {:,}".format(
                    int(a.sum()), int(b.sum()), int(c.sum())
                )
            )
            report.append(
                "  files judged SWAPPED: {} of {}".format(
                    len(swap_files), int(votes.shape[0])
                )
            )
        else:
            report.append(
                "  date mode: perrow | swapped rows: {:,} | correct rows: {:,}".format(
                    int(a.sum()), int(b.sum())
                )
            )

        df["publish_month"], df["publish_day"] = month, day

    return df.drop(columns=["raw_publish_month", "raw_publish_day"])


def clean_books(input_dir: str, report: list, date_mode: str = "perrow") -> pd.DataFrame:
    paths = sorted(glob.glob(os.path.join(input_dir, "book*.csv")))
    if not paths:
        sys.exit(f"ERROR: no book*.csv files found in {input_dir}")

    report.append(f"BOOKS: found {len(paths)} chunk file(s)")

    frames = []
    header_variants = Counter()
    for path in paths:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            header_variants[fh.readline().strip()] += 1
        frames.append(load_book_file(path, report))

    report.append(f"  distinct header variants encountered: {len(header_variants)}")

    df = pd.concat(frames, ignore_index=True)
    raw_rows = len(df)
    report.append(f"  rows parsed from CSV: {raw_rows:,}")

    # --- type coercion -----------------------------------------------------
    df["id"] = to_int(df["id"])
    df["rating"] = to_float(df["rating"])
    df["publish_year"] = to_int(df["publish_year"])
    df = resolve_dates(df, date_mode, report)
    df["pages_number"] = to_int(df["pages_number"])
    df["counts_of_review"] = to_int(df["counts_of_review"])
    df["count_of_text_reviews"] = to_int(df["count_of_text_reviews"])

    for col in DIST_COLUMNS:
        df[col] = strip_dist_prefix(df[col])

    df["isbn"] = clean_isbn(df["isbn"])

    for col in ("name", "authors", "publisher", "language", "description"):
        df[col] = df[col].astype("string").str.strip()

    df["language_normalised"] = normalise_language(df["language"])

    # --- validity sweeps ---------------------------------------------------
    # Rows with no usable Id are unusable as records.
    no_id = df["id"].isna().sum()
    if no_id:
        df = df[df["id"].notna()]
        report.append(f"  dropped {no_id:,} row(s) with no parseable Id")

    # Month/day sanity. The swap is already handled by the crossed HEADER_MAP;
    # anything still out of range is genuine junk, so null it rather than
    # silently keeping an impossible date.
    bad_month = df["publish_month"].notna() & ~df["publish_month"].between(1, 12)
    bad_day = df["publish_day"].notna() & ~df["publish_day"].between(1, 31)
    if bad_month.any():
        report.append(f"  nulled {int(bad_month.sum()):,} out-of-range publish_month value(s)")
        df.loc[bad_month, "publish_month"] = pd.NA
    if bad_day.any():
        report.append(f"  nulled {int(bad_day.sum()):,} out-of-range publish_day value(s)")
        df.loc[bad_day, "publish_day"] = pd.NA

    bad_year = df["publish_year"].notna() & ~df["publish_year"].between(1000, 2026)
    if bad_year.any():
        report.append(f"  nulled {int(bad_year.sum()):,} implausible publish_year value(s)")
        df.loc[bad_year, "publish_year"] = pd.NA

    bad_rating = df["rating"].notna() & ~df["rating"].between(0, 5)
    if bad_rating.any():
        report.append(f"  nulled {int(bad_rating.sum()):,} out-of-range rating value(s)")
        df.loc[bad_rating, "rating"] = pd.NA

    bad_pages = df["pages_number"].notna() & ~df["pages_number"].between(1, 20000)
    if bad_pages.any():
        report.append(f"  nulled {int(bad_pages.sum()):,} implausible pages_number value(s)")
        df.loc[bad_pages, "pages_number"] = pd.NA

    # --- duplicates --------------------------------------------------------
    dupes = int(df.duplicated(subset=["id"]).sum())
    if dupes:
        df = df.drop_duplicates(subset=["id"], keep="first")
        report.append(f"  dropped {dupes:,} duplicate Id row(s)")

    df = df.sort_values("id").reset_index(drop=True)

    report.append(f"  FINAL books rows: {len(df):,}")
    report.append(f"  books with ISBN:  {int(df['isbn'].notna().sum()):,}")
    report.append(f"  books with description: {int(df['description'].notna().sum()):,}")
    return df


# --------------------------------------------------------------------------
# User ratings
# --------------------------------------------------------------------------

def clean_user_ratings(input_dir: str, report: list) -> pd.DataFrame:
    paths = sorted(glob.glob(os.path.join(input_dir, "user_rating*.csv")))
    if not paths:
        report.append("USER RATINGS: no user_rating*.csv files found - skipped")
        return pd.DataFrame(columns=[c for c, _ in USER_RATINGS_BQ_SCHEMA])

    report.append(f"USER RATINGS: found {len(paths)} file(s)")

    frames = []
    for path in paths:
        d = read_csv_resilient(path)
        d = d.rename(columns={c: c.strip().lower() for c in d.columns})
        d = d.rename(columns={"id": "user_id", "name": "book_title", "rating": "rating_label"})
        frames.append(d)

    df = pd.concat(frames, ignore_index=True)
    report.append(f"  rows parsed from CSV: {len(df):,}")

    df["user_id"] = to_int(df["user_id"])
    df["book_title"] = df["book_title"].astype("string").str.strip()
    df["rating_label"] = df["rating_label"].astype("string").str.strip()

    # Drop placeholder rows before mapping.
    lowered = df["rating_label"].str.lower().fillna("")
    drop_mask = pd.Series(False, index=df.index)
    for pat in RATING_DROP_PATTERNS:
        drop_mask |= lowered.str.contains(re.escape(pat), na=False)
    if drop_mask.any():
        report.append(f"  dropped {int(drop_mask.sum()):,} placeholder 'no rating' row(s)")
        df = df[~drop_mask]
        lowered = lowered[~drop_mask]

    df["rating"] = lowered.map(RATING_LABEL_MAP).astype("Int64")

    unmapped_labels = sorted(
        set(lowered[df["rating"].isna()].dropna().unique())
    )
    if unmapped_labels:
        preview = unmapped_labels[:8]
        report.append(
            f"  {int(df['rating'].isna().sum()):,} row(s) had unrecognised rating "
            f"label(s), rating left NULL. Examples: {preview}"
        )

    df["book_title_normalised"] = normalise_title(df["book_title"])

    no_title = int(df["book_title"].isna().sum())
    if no_title:
        df = df[df["book_title"].notna()]
        report.append(f"  dropped {no_title:,} row(s) with no book title")

    before = len(df)
    df = df.drop_duplicates(subset=["user_id", "book_title", "rating_label"])
    if before - len(df):
        report.append(f"  dropped {before - len(df):,} exact duplicate rating row(s)")

    df = df.reindex(columns=[c for c, _ in USER_RATINGS_BQ_SCHEMA])
    df = df.sort_values(["user_id", "book_title"]).reset_index(drop=True)

    report.append(f"  FINAL user_ratings rows: {len(df):,}")
    report.append(f"  distinct users:  {int(df['user_id'].nunique()):,}")
    report.append(f"  distinct titles: {int(df['book_title_normalised'].nunique()):,}")
    return df


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Clean the Goodreads 10M Kaggle dump.")
    ap.add_argument("--input", required=True, help="folder holding the raw CSVs")
    ap.add_argument("--output", default="./clean", help="folder for cleaned output")
    ap.add_argument(
        "--parquet",
        action="store_true",
        help="also write Parquet (requires pyarrow; faster BigQuery loads)",
    )
    ap.add_argument(
        "--date-mode",
        choices=["perrow", "swap", "asis"],
        default="perrow",
        help="how to resolve the PublishMonth/PublishDay swap (default: perrow)",
    )
    args = ap.parse_args()

    input_dir = os.path.expanduser(args.input)
    output_dir = os.path.expanduser(args.output)
    os.makedirs(output_dir, exist_ok=True)

    report = []
    report.append("Goodreads cleaning report")
    report.append("=" * 60)
    report.append(f"input : {input_dir}")
    report.append(f"output: {output_dir}")
    report.append("")

    print("Cleaning books (this takes a minute on 1.2 GB)...")
    books = clean_books(input_dir, report, args.date_mode)
    report.append("")

    print("Cleaning user ratings...")
    ratings = clean_user_ratings(input_dir, report)
    report.append("")

    # --- coverage check between the two tables ----------------------------
    if len(ratings):
        book_titles = set(normalise_title(books["name"]).dropna())
        rated = set(ratings["book_title_normalised"].dropna())
        overlap = len(rated & book_titles)
        pct = 100.0 * overlap / max(len(rated), 1)
        report.append("JOIN COVERAGE (normalised title match)")
        report.append(f"  rated titles matching a book row: {overlap:,} / {len(rated):,} ({pct:.1f}%)")
        report.append("  NOTE: user_ratings carry no book Id, so this is the best")
        report.append("        join available. Treat the two tables as largely")
        report.append("        independent for analysis.")
        report.append("")

    books_csv = os.path.join(output_dir, "books_clean.csv")
    ratings_csv = os.path.join(output_dir, "user_ratings_clean.csv")

    print(f"Writing {books_csv} ...")
    books.to_csv(books_csv, index=False)
    print(f"Writing {ratings_csv} ...")
    ratings.to_csv(ratings_csv, index=False)

    if args.parquet:
        try:
            books.to_parquet(os.path.join(output_dir, "books_clean.parquet"), index=False)
            ratings.to_parquet(os.path.join(output_dir, "user_ratings_clean.parquet"), index=False)
            report.append("Parquet written alongside CSV.")
        except Exception as exc:  # pyarrow missing or failed
            report.append(f"Parquet skipped: {exc}")
            print(f"  (parquet skipped: {exc})")

    write_bq_schema(os.path.join(output_dir, "books_schema.json"), BOOKS_BQ_SCHEMA)
    write_bq_schema(os.path.join(output_dir, "user_ratings_schema.json"), USER_RATINGS_BQ_SCHEMA)

    report_path = os.path.join(output_dir, "cleaning_report.txt")
    with open(report_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(report) + "\n")

    print("\n" + "\n".join(report))
    print(f"\nDone. Report saved to {report_path}")


if __name__ == "__main__":
    main()
