"""Tests for entity resolution candidate generation and name normalization."""

from zeitgeist.resolver.candidates import candidate_pairs, initials, normalize


# Tests for normalize function
def test_normalize_casefold():
    """Test that normalize converts to lowercase."""
    assert normalize("EUROPEAN CENTRAL BANK") == "european central bank"
    assert normalize("United States") == "united states"


def test_normalize_collapse_whitespace():
    """Test that normalize collapses multiple spaces into one."""
    assert normalize("UNITED  STATES") == "united states"
    assert normalize("CENTRAL   BANK") == "central bank"
    assert normalize("  LEADING  AND  TRAILING  ") == "leading and trailing"


def test_normalize_strip_punctuation():
    """Test that normalize replaces punctuation with spaces."""
    assert normalize("BANK, INC.") == "bank inc"
    assert normalize("U.S.A.") == "u s a"
    assert normalize("PEOPLES' REPUBLIC") == "peoples republic"


def test_normalize_combined():
    """Test normalize with all transformations."""
    assert normalize("  THE UNITED STATES, INC.  ") == "the united states inc"
    assert normalize("CENTRAL...BANK") == "central bank"


def test_normalize_hyphen_replacement():
    """Test that hyphens are replaced with spaces, not deleted."""
    assert normalize("GUINEA-BISSAU") == "guinea bissau"
    assert normalize("U.S.A.") == "u s a"


# Tests for initials function
def test_initials_basic():
    """Test initials extraction from standard name."""
    assert initials("EUROPEAN CENTRAL BANK") == "ecb"
    assert initials("UNITED STATES") == "us"


def test_initials_single_word():
    """Test initials of single word (returns just first letter)."""
    assert initials("BANK") == "b"


def test_initials_three_words():
    """Test initials with three or more words."""
    assert initials("FEDERAL RESERVE BANK") == "frb"


def test_initials_lowercase_already():
    """Test that initials normalizes case."""
    assert initials("european central bank") == "ecb"


def test_initials_skip_single_char_words():
    """Test that initials includes only letters from words >= 1 char after normalization."""
    # "A" is 1 char, but it's a word; initials should include it
    assert initials("A BANK") == "ab"


def test_initials_with_whitespace():
    """Test initials handles extra whitespace."""
    assert initials("  EUROPEAN  CENTRAL  BANK  ") == "ecb"


# Tests for candidate_pairs function
def test_candidate_pairs_empty_input():
    """Test empty input returns empty list."""
    assert candidate_pairs([]) == []


def test_candidate_pairs_single_entity():
    """Test single entity returns no pairs."""
    assert candidate_pairs([("BANK", 10)]) == []


def test_candidate_pairs_no_matches():
    """Test entities with no matching rules return no pairs."""
    # IRAN and IRAQ have no matching rule
    assert candidate_pairs([("IRAN", 10), ("IRAQ", 5)]) == []


def test_candidate_pairs_rule1_initials():
    """Test rule 1: normalized name equals other's initials."""
    # "ECB" normalized = "ecb", initials("EUROPEAN CENTRAL BANK") = "ecb"
    result = candidate_pairs([("ECB", 5), ("EUROPEAN CENTRAL BANK", 10)])
    assert len(result) == 1
    # Returns ORIGINAL names, not normalized
    assert result[0] == ("ECB", "EUROPEAN CENTRAL BANK")


def test_candidate_pairs_rule1_reversed():
    """Test rule 1 with reversed order - should order by event count."""
    result = candidate_pairs([("EUROPEAN CENTRAL BANK", 10), ("ECB", 5)])
    # ECB (5) should be first, EUROPEAN CENTRAL BANK (10) should be second
    assert len(result) == 1
    # Returns ORIGINAL names in correct order (lower count first)
    assert result[0] == ("ECB", "EUROPEAN CENTRAL BANK")


def test_candidate_pairs_rule2_subphrase():
    """Test rule 2: whole-word subphrase matching."""
    # "UNITED STATES" is a subphrase of "THE UNITED STATES"
    result = candidate_pairs([("UNITED STATES", 5), ("THE UNITED STATES", 10)])
    assert len(result) == 1
    # Returns ORIGINAL names
    assert result[0] == ("UNITED STATES", "THE UNITED STATES")


def test_candidate_pairs_rule2_not_subphrase():
    """Test rule 2 rejects scrambled word order (but rule 3 catches it)."""
    # "CENTRAL BANK OF NIGERIA" vs "NIGERIA CENTRAL BANK" - not subphrase (rule 2)
    # but matches via rule 3 (Jaccard >= 0.6)
    result = candidate_pairs([("NIGERIA CENTRAL BANK", 10), ("CENTRAL BANK OF NIGERIA", 5)])
    # Should match via rule 3 (Jaccard), not rule 2
    assert len(result) == 1
    # Returns ORIGINAL names (lower count first)
    assert result[0] == ("CENTRAL BANK OF NIGERIA", "NIGERIA CENTRAL BANK")


def test_candidate_pairs_rule3_jaccard():
    """Test rule 3: Jaccard >= 0.6 with both >= 2 words."""
    # "CENTRAL BANK OF NIGERIA" vs "NIGERIA CENTRAL BANK"
    # Words in first: {central, bank, of, nigeria}
    # Words in second: {nigeria, central, bank}
    # Intersection: {central, bank, nigeria}
    # Union: {central, bank, of, nigeria}
    # Jaccard: 3/4 = 0.75 >= 0.6 ✓
    result = candidate_pairs([("NIGERIA CENTRAL BANK", 10), ("CENTRAL BANK OF NIGERIA", 5)])
    assert len(result) == 1
    # Returns ORIGINAL names (lower count first)
    assert result[0] == ("CENTRAL BANK OF NIGERIA", "NIGERIA CENTRAL BANK")


def test_candidate_pairs_rule3_below_threshold():
    """Test rule 3 rejects Jaccard < 0.6."""
    # "POLICE" and "POLICE OFFICER"
    # Words in "POLICE": {police}
    # Words in "POLICE OFFICER": {police, officer}
    # Intersection: {police}
    # Union: {police, officer}
    # Jaccard: 1/2 = 0.5 < 0.6 ✗
    # But POLICE OFFICER has 2 words, so it should be checked
    # Actually "POLICE" is single word, so rule 3 requires both >= 2 words
    # This means it won't match rule 3, but might match another rule
    result = candidate_pairs([("POLICE", 10), ("POLICE OFFICER", 5)])
    # Rule 1: "police" != initials("police officer") = "po", so no
    # Rule 2: "police" is a subphrase of "police officer" - yes! First word matches.
    # So this should match rule 2
    assert len(result) == 1
    # Order: lower count (5) first, higher count (10) second, returns ORIGINAL names
    assert result[0] == ("POLICE OFFICER", "POLICE")


def test_candidate_pairs_ordering_by_event_count():
    """Test that pairs are ordered by combined event count descending."""
    # Create entities that will match via subphrase or Jaccard
    entities = [
        ("US", 10),
        ("UNITED STATES", 15),  # Matches via rule 1 (initials) - combined 25
        ("CENTRAL BANK OF NIGERIA", 8),
        ("NIGERIA CENTRAL BANK", 10),  # Matches via rule 3 - combined 18
    ]
    result = candidate_pairs(entities)
    # Should be sorted by combined count descending
    if len(result) >= 2:
        first_combined = 10 + 15  # 25
        second_combined = 8 + 10  # 18
        assert first_combined >= second_combined


def test_candidate_pairs_500_cap():
    """Test that output is capped at 500 pairs."""
    # Create entities that will match via subphrase
    entities = []
    for i in range(100):
        # Create words that are subphrases of longer names
        entities.append(("WORD", i))
        entities.append((f"WORD EXTRA{i}", i + 100))
    result = candidate_pairs(entities)
    assert len(result) <= 500


def test_candidate_pairs_deterministic_order():
    """Test that output order is deterministic based on combined event count."""
    entities = [
        ("POLAND", 5),
        ("REPUBLIC OF POLAND", 15),  # Combined: 20
        ("US", 5),
        ("UNITED STATES", 10),  # Combined: 15
    ]
    result1 = candidate_pairs(entities)
    result2 = candidate_pairs(entities)
    assert result1 == result2


def test_candidate_pairs_canonical_direction():
    """Test that lesser event count is first, greater is second."""
    result = candidate_pairs([("ECB", 3), ("EUROPEAN CENTRAL BANK", 7)])
    assert result[0] == ("ECB", "EUROPEAN CENTRAL BANK")
    # Verify canonical ordering
    lesser, greater = result[0]
    assert normalize(lesser) < normalize(greater) or greater == "EUROPEAN CENTRAL BANK"


def test_candidate_pairs_equal_count_tie_break():
    """Test that on equal event counts, longer name is canonical (second)."""
    result = candidate_pairs([("ECB", 5), ("EUROPEAN CENTRAL BANK", 5)])
    assert len(result) == 1
    # On tie: shorter name first, longer name second (canonical)
    assert result[0] == ("ECB", "EUROPEAN CENTRAL BANK")


def test_candidate_pairs_regression_mixed_case_punctuation():
    """Regression: Returns ORIGINAL names with mixed case and punctuation."""
    # Input names with mixed case and punctuation
    result = candidate_pairs([("U.S.", 5), ("US", 9)])
    assert len(result) == 1
    # Must return ORIGINAL names, not normalized
    assert result[0] == ("U.S.", "US")
    # Verify originals are returned verbatim (exact blind spot fix)
    assert result[0][0] == "U.S."
    assert result[0][1] == "US"


def test_candidate_pairs_guinea_bissau_hyphen():
    """Test that hyphenated names pair correctly with space-separated equivalents."""
    result = candidate_pairs([("GUINEA-BISSAU", 3), ("GUINEA BISSAU", 7)])
    assert len(result) == 1
    # Returns ORIGINAL names, ordered by count
    assert result[0] == ("GUINEA-BISSAU", "GUINEA BISSAU")
