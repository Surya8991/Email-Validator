import csv
import io

import aiofiles

_FORMULA_CHARS = frozenset(("=", "+", "-", "@", "\t", "\r"))


def csv_safe(v: str) -> str:
    """Prefix formula-trigger characters to prevent CSV injection (CWE-1236)."""
    s = str(v)
    return ("'" + s) if s and s[0] in _FORMULA_CHARS else s


async def parse_csv_emails(filepath: str, email_column: str = "") -> list[tuple[int, str, dict]]:
    """Returns list of (row_index, email, original_row_dict)."""
    rows = []
    async with aiofiles.open(filepath, encoding="utf-8-sig") as f:
        content = await f.read()
    reader = csv.DictReader(io.StringIO(content))
    if not reader.fieldnames:
        return rows
    # Auto-detect email column
    col = email_column
    if not col:
        for field in reader.fieldnames:
            if "email" in field.lower():
                col = field
                break
    if not col and reader.fieldnames:
        col = reader.fieldnames[0]
    for i, row in enumerate(reader):
        email = row.get(col, "").strip()
        if email:
            rows.append((i, email, dict(row)))
    return rows


def write_results_csv(
    original_rows: list[dict],
    email_col: str,
    results: list[dict],  # [{email, verdict, providers:{...}}]
    output_path: str,
) -> None:
    if not original_rows:
        return
    result_map = {r["email"].lower(): r for r in results}
    extra_cols = ["verdict", "is_valid", "is_risky", "is_invalid", "providers_checked"]
    fieldnames = list(original_rows[0].keys()) + extra_cols

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in original_rows:
            email = row.get(email_col, "").strip().lower()
            result = result_map.get(email, {})
            verdict = result.get("verdict", "unknown")
            row["verdict"] = verdict
            row["is_valid"] = "1" if verdict == "valid" else "0"
            row["is_risky"] = "1" if verdict == "risky" else "0"
            row["is_invalid"] = "1" if verdict == "invalid" else "0"
            row["providers_checked"] = ",".join(result.get("providers", {}).keys())
            writer.writerow(row)
