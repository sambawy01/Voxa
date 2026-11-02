//! Letter-to-sound rules: the last resort for words that are not in the dictionary and
//! cannot be derived from one (new names, invented words). Approximate by nature, but they
//! guarantee no word is silently dropped. Output uses misaki's symbols (note `ɡ`, not `g`;
//! `ɹ`, not `r`; A=eɪ, I=aɪ, O=oʊ, W=aʊ, Y=ɔɪ).

/// Vowel phonemes in misaki's symbol set (used for stress placement and suffix rules).
pub const VOWELS: &str = "AIOWYaæɑɒɔəɚɛɜeɪiuʊʌᵊᵻɐo";

fn is_vowel_letter(c: u8) -> bool {
    matches!(c, b'a' | b'e' | b'i' | b'o' | b'u')
}

fn long_vowel(c: u8) -> &'static str {
    match c {
        b'a' => "A",
        b'e' => "i",
        b'i' => "I",
        b'o' => "O",
        _ => "u",
    }
}

fn short_vowel(c: u8) -> &'static str {
    match c {
        b'a' => "æ",
        b'e' => "ɛ",
        b'i' => "ɪ",
        b'o' => "ɑ",
        _ => "ʌ",
    }
}

/// Strips accents from common Latin letters and drops anything that is not a-z.
pub fn ascii_fold(word: &str) -> String {
    word.chars()
        .flat_map(char::to_lowercase)
        .filter_map(|c| match c {
            'a'..='z' => Some(c),
            'à' | 'á' | 'â' | 'ã' | 'ä' | 'å' => Some('a'),
            'è' | 'é' | 'ê' | 'ë' => Some('e'),
            'ì' | 'í' | 'î' | 'ï' => Some('i'),
            'ò' | 'ó' | 'ô' | 'õ' | 'ö' | 'ø' => Some('o'),
            'ù' | 'ú' | 'û' | 'ü' => Some('u'),
            'ç' => Some('c'),
            'ñ' => Some('n'),
            'ý' | 'ÿ' => Some('y'),
            _ => None,
        })
        .collect()
}

/// Pronounces a word by rule. Returns an empty string only if it has no letters at all.
pub fn letter_to_sound(word: &str) -> String {
    let w = ascii_fold(word);
    let b = w.as_bytes();
    let n = b.len();
    let mut out = String::new();
    let mut i = 0;
    let at = |j: usize| -> u8 {
        if j < n {
            b[j]
        } else {
            0
        }
    };
    let rest = |j: usize| -> &str { &w[j.min(n)..] };
    while i < n {
        let c = b[i];
        let r = rest(i);
        // Multi-letter patterns, longest first.
        let multi: &[(&str, &str)] = &[
            ("tion", "ʃən"),
            ("sion", "ʒən"),
            ("ture", "ʧəɹ"),
            ("eigh", "A"),
            ("augh", "ɔ"),
            ("ough", "O"),
            ("igh", "I"),
            ("tch", "ʧ"),
            ("dge", "ʤ"),
            ("sch", "sk"),
            ("ph", "f"),
            ("sh", "ʃ"),
            ("ch", "ʧ"),
            ("th", "θ"),
            ("ck", "k"),
            ("ng", "ŋ"),
            ("wh", "w"),
            ("qu", "kw"),
            ("ai", "A"),
            ("ay", "A"),
            ("ee", "i"),
            ("ea", "i"),
            ("ei", "A"),
            ("oa", "O"),
            ("oo", "u"),
            ("ou", "W"),
            ("oi", "Y"),
            ("oy", "Y"),
            ("au", "ɔ"),
            ("aw", "ɔ"),
            ("ew", "u"),
            ("ar", "ɑɹ"),
            ("or", "ɔɹ"),
            ("ir", "ɜɹ"),
            ("ur", "ɜɹ"),
        ];
        if i == 0 && (r.starts_with("kn") || r.starts_with("gn")) {
            out.push('n');
            i += 2;
            continue;
        }
        if i == 0 && r.starts_with("wr") {
            out.push('ɹ');
            i += 2;
            continue;
        }
        if r == "mb" {
            out.push('m');
            break;
        }
        if r.starts_with("gh") {
            if i == 0 {
                out.push('ɡ');
            }
            i += 2;
            continue;
        }
        if r.starts_with("ow") {
            out.push(if i + 2 == n { 'O' } else { 'W' });
            i += 2;
            continue;
        }
        if r.starts_with("er") {
            // Unstressed "er" (water, never) is the common case.
            out.push_str(if i == 0 { "ɜɹ" } else { "əɹ" });
            i += 2;
            continue;
        }
        if r == "ey" || r == "ie" {
            out.push_str(if n <= 3 { "I" } else { "i" });
            break;
        }
        if r == "es" && i > 1 && matches!(b[i - 1], b's' | b'x' | b'z') {
            out.push_str("ᵻz");
            break;
        }
        if let Some((pat, ph)) = multi.iter().find(|(p, _)| r.starts_with(p)) {
            out.push_str(ph);
            i += pat.len();
            continue;
        }
        if is_vowel_letter(c) {
            // Magic e: vowel + one consonant + final e ("make", "bike", "note").
            let magic = i + 2 < n + 1
                && at(i + 1) != 0
                && !is_vowel_letter(at(i + 1))
                && at(i + 1) != b'r'
                && at(i + 2) == b'e'
                && i + 3 == n;
            if magic {
                out.push_str(long_vowel(c));
            } else if c == b'e' && i + 1 == n && n > 2 {
                // Silent final e.
            } else if c == b'a' && i + 1 == n && n > 1 {
                out.push('ə');
            } else if c == b'o' && i + 1 == n {
                out.push('O');
            } else {
                out.push_str(short_vowel(c));
            }
            i += 1;
            continue;
        }
        // Consonants. Skip the second of a doubled letter.
        if i > 0 && b[i - 1] == c && !is_vowel_letter(c) {
            i += 1;
            continue;
        }
        let soft = matches!(at(i + 1), b'e' | b'i' | b'y');
        match c {
            b'c' => out.push(if soft { 's' } else { 'k' }),
            b'g' => out.push(if soft && i + 1 < n { 'ʤ' } else { 'ɡ' }),
            b'j' => out.push('ʤ'),
            b'q' => out.push('k'),
            b'r' => out.push('ɹ'),
            b'x' => out.push_str(if i == 0 { "z" } else { "ks" }),
            b'y' => {
                if i == 0 {
                    out.push('j');
                } else if i + 1 == n {
                    out.push_str(if n <= 3 { "I" } else { "i" });
                } else {
                    out.push('ɪ');
                }
            }
            b'z' => out.push('z'),
            other => out.push(other as char),
        }
        i += 1;
    }
    add_primary_stress(&out)
}

/// Puts a primary stress mark before the first vowel if the string has none.
pub fn add_primary_stress(p: &str) -> String {
    if p.contains('ˈ') {
        return p.to_string();
    }
    match p.char_indices().find(|(_, c)| VOWELS.contains(*c)) {
        Some((i, _)) => format!("{}ˈ{}", &p[..i], &p[i..]),
        None => p.to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn common_patterns() {
        assert_eq!(letter_to_sound("cat"), "kˈæt");
        assert_eq!(letter_to_sound("make"), "mˈAk");
        assert_eq!(letter_to_sound("nation"), "nˈæʃən");
        assert_eq!(letter_to_sound("knight"), "nˈIt");
        assert_eq!(letter_to_sound("phone"), "fˈOn");
        assert_eq!(letter_to_sound("city"), "sˈɪti");
        assert_eq!(letter_to_sound("gem"), "ʤˈɛm");
        assert_eq!(letter_to_sound("bigger"), "bˈɪɡəɹ");
    }

    #[test]
    fn invented_words_always_get_phonemes() {
        for w in ["Voxa", "blorptastic", "Zxqwv", "Brrr", "Nguyễn", "x"] {
            let p = letter_to_sound(w);
            assert!(!p.is_empty(), "{w} produced nothing");
            assert!(
                !p.contains('g') && !p.contains('r'),
                "{w}: {p} uses ASCII g/r"
            );
        }
        assert_eq!(letter_to_sound("Voxa"), "vˈɑksə");
    }

    #[test]
    fn folds_accents_and_drops_non_letters() {
        assert_eq!(ascii_fold("Café-Noël!"), "cafenoel");
        assert_eq!(letter_to_sound("日本"), "");
    }

    #[test]
    fn stress_is_added_once() {
        assert_eq!(add_primary_stress("kæt"), "kˈæt");
        assert_eq!(add_primary_stress("kˈæt"), "kˈæt");
        assert_eq!(add_primary_stress("st"), "st");
    }
}
