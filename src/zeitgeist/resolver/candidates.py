"""Candidate generation for entity resolution via name matching rules."""

import re


def normalize(name: str) -> str:
    """Normalize a name for comparison.

    Transformation steps:
    1. casefold to lowercase
    2. replace punctuation with spaces (preserve word boundaries)
    3. collapse multiple whitespace to single space
    4. strip leading and trailing whitespace

    Args:
        name: Input name string.

    Returns:
        Normalized name in lowercase with punctuation replaced by spaces
        and whitespace collapsed.
    """
    # Casefold to lowercase
    name = name.casefold()
    # Replace punctuation with spaces (preserve word boundaries)
    name = re.sub(r"[^\w\s]", " ", name, flags=re.UNICODE)
    # Collapse multiple whitespace to single space
    name = re.sub(r"\s+", " ", name)
    # Strip leading and trailing whitespace
    name = name.strip()
    return name


def initials(name: str) -> str:
    """Extract initials from a name.

    Returns the first letter of each word (>=1 character after normalization)
    in lowercase.

    Args:
        name: Input name string.

    Returns:
        String of lowercase initials, one per word.

    Example:
        initials("EUROPEAN CENTRAL BANK") → "ecb"
        initials("UNITED STATES") → "us"
    """
    normalized = normalize(name)
    words = normalized.split()
    return "".join(word[0] for word in words if word)


def candidate_pairs(
    entities: list[tuple[str, int]],
) -> list[tuple[str, str]]:
    """Generate plausible same-entity candidate pairs.

    Identifies pairs of entities that may refer to the same real-world entity
    using three blocking rules. Output is ordered by combined event count (descending)
    and capped at 500 pairs.

    Blocking rules (a pair qualifies if ANY hold):
    1. Initials match: one normalized name equals the other's initials.
       Example: "ECB" ↔ "EUROPEAN CENTRAL BANK"
    2. Whole-word subphrase: one normalized name is a word-level subsequence
       of the other's normalized words.
       Example: "UNITED STATES" ↔ "THE UNITED STATES" (yes)
       Example: "NIGERIA CENTRAL BANK" ↔ "CENTRAL BANK OF NIGERIA" (no)
    3. Token-set Jaccard: word-set overlap >= 0.6, both names >= 2 words.
       Example: "CENTRAL BANK OF NIGERIA" ↔ "NIGERIA CENTRAL BANK"
       Jaccard = intersection / union = 3 / 4 = 0.75 >= 0.6 ✓

    Args:
        entities: List of (name, event_count) tuples.

    Returns:
        List of (lesser, greater) ORIGINAL name pairs ordered by combined
        event count (desc), capped at 500. Lesser/greater are determined by
        event_count (lower first), with ties broken by longer name being
        canonical (greater).
    """
    if not entities:
        return []

    # Precompute metadata for each entity (norm, initials, words, wordset)
    metadata = []
    for name, count in entities:
        norm = normalize(name)
        init = initials(name)
        words = norm.split()
        wordset = set(words)
        metadata.append((norm, init, words, wordset))

    # Find matching pairs
    pairs = []
    seen = set()

    for i, (name1, count1) in enumerate(entities):
        norm1, init1, words1, wordset1 = metadata[i]

        for j, (name2, count2) in enumerate(entities):
            if i >= j:  # Avoid duplicates and self-pairs
                continue

            norm2, init2, words2, wordset2 = metadata[j]

            # Check blocking rules
            # Rule 1: One normalized name equals the other's initials
            rule1_match = norm1 == init2 or norm2 == init1

            # Rule 2: Whole-word subphrase (word-level subsequence)
            rule2_match = _is_word_subsequence(words1, words2) or _is_word_subsequence(
                words2, words1
            )

            # Rule 3: Jaccard >= 0.6 (both >= 2 words)
            rule3_match = False
            if len(words1) >= 2 and len(words2) >= 2:
                intersection = len(wordset1 & wordset2)
                union = len(wordset1 | wordset2)
                jaccard = intersection / union if union > 0 else 0.0
                rule3_match = jaccard >= 0.6

            # If any rule matches, add the pair (using ORIGINAL names)
            if rule1_match or rule2_match or rule3_match:
                # Determine canonical order: lower event_count first,
                # ties: longer name is canonical (second)
                if count1 < count2:
                    lesser_name, greater_name = name1, name2
                    combined = count1 + count2
                elif count1 > count2:
                    lesser_name, greater_name = name2, name1
                    combined = count1 + count2
                else:
                    # Tie on count: longer original name is canonical (second)
                    if len(name1) <= len(name2):
                        lesser_name, greater_name = name1, name2
                    else:
                        lesser_name, greater_name = name2, name1
                    combined = count1 + count2

                pair_key = (lesser_name, greater_name)
                if pair_key not in seen:
                    seen.add(pair_key)
                    pairs.append((pair_key, combined))

    # Sort by combined event count descending, then by pair for determinism
    pairs.sort(key=lambda x: (-x[1], x[0]))

    # Cap at 500 and return just the pairs
    return [pair for pair, _ in pairs[:500]]


def _is_word_subsequence(words1: list[str], words2: list[str]) -> bool:
    """Check if words1 is a contiguous word subsequence of words2.

    Args:
        words1: Potential subsequence.
        words2: Potential sequence.

    Returns:
        True if words1 appears as a contiguous subsequence in words2.

    Example:
        _is_word_subsequence(["united", "states"], ["the", "united", "states"]) → True
        _is_word_subsequence(["nigeria", "central", "bank"],
                             ["central", "bank", "of", "nigeria"]) → False
    """
    if not words1:
        return False
    if len(words1) > len(words2):
        return False

    m, n = len(words1), len(words2)
    for i in range(n - m + 1):
        if words2[i : i + m] == words1:
            return True
    return False
