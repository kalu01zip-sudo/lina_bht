def compress_text(text: str) -> str:
    words = text.lower().split()

    # remove useless words
    stopwords = {"to", "the", "and", "a", "is", "am", "i"}
    filtered = [w for w in words if w not in stopwords]

    # take first 2–3 meaningful words
    return " ".join(filtered[:3])

def expand_and_clean(values: list[str]) -> list[str]:
    result = []

    for item in values:
        parts = item.split(",")  # split comma

        for p in parts:
            clean = p.strip().lower()  # normalize
            if clean:
                result.append(clean)

    return result