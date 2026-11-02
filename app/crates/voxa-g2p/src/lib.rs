//! Voxa's English G2P: text in, misaki phonemes out, for Kokoro. No GPL code or data.
//!
//! Pipeline per sentence:
//! 1. [`normalize`]: numbers, money, dates, times, symbols and abbreviations become words.
//! 2. Tokenise into words and the punctuation Kokoro understands.
//! 3. Each word is pronounced by the first step that works:
//!    dictionary → word forms (possessive, plural, -ed, -ing, -ly, -ness) → hyphen parts →
//!    letter names for acronyms → two dictionary words (compound) → letter-to-sound rules.
//!
//! Every word that has a letter in it gets phonemes. [`EnglishG2p::analyse`] reports which
//! step pronounced each word, so tests can hold that line.

pub mod lexicon;
pub mod normalize;
pub mod numbers;
pub mod rules;

use lexicon::Lexicon;
use rules::{add_primary_stress, letter_to_sound, VOWELS};
use voxa_core::{EngineError, Phonemizer};

/// Which step pronounced a word.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Source {
    Dictionary,
    WordForm,
    Hyphenated,
    Spelled,
    Compound,
    Rules,
}

#[derive(Debug, Clone, PartialEq)]
pub enum Token {
    Word {
        text: String,
        phonemes: String,
        source: Source,
    },
    Punct(char),
}

/// Punctuation Kokoro has tokens for. Others become word breaks.
const PUNCT: &str = ",.!?;:—…\"()“”";

pub struct EnglishG2p {
    lex: Lexicon,
}

impl EnglishG2p {
    pub fn new(lex: Lexicon) -> Self {
        Self { lex }
    }

    /// Loads misaki's `us_silver.json` and `us_gold.json` from a package directory.
    pub fn from_dir(dir: &std::path::Path) -> Result<Self, EngineError> {
        let read = |name: &str| {
            std::fs::read(dir.join(name)).map_err(|e| EngineError::Failed(format!("{name}: {e}")))
        };
        let lex = Lexicon::from_json(&[&read("us_silver.json")?, &read("us_gold.json")?])
            .map_err(|e| EngineError::Failed(format!("lexicon: {e}")))?;
        Ok(Self::new(lex))
    }

    /// Normalises and pronounces a sentence, reporting each token.
    pub fn analyse(&self, sentence: &str) -> Vec<Token> {
        let text = normalize::normalize(sentence);
        let mut out = Vec::new();
        for raw in tokenize(&text) {
            match raw {
                Raw::Punct(c) => out.push(Token::Punct(c)),
                Raw::Word(w) => {
                    if let Some((phonemes, source)) = self.word(&w) {
                        out.push(Token::Word {
                            text: w,
                            phonemes,
                            source,
                        });
                    }
                }
            }
        }
        out
    }

    /// Pronounces one word; `None` only when it has no letters to pronounce.
    pub fn word(&self, w: &str) -> Option<(String, Source)> {
        let w = w.trim_matches('\'');
        if w.is_empty() {
            return None;
        }
        if let Some(p) = self.lex.get(w) {
            return Some((p.to_string(), Source::Dictionary));
        }
        if let Some(p) = self.word_form(w) {
            return Some((p, Source::WordForm));
        }
        if w.contains('-') {
            let parts: Vec<String> = w
                .split('-')
                .filter(|p| !p.is_empty())
                .filter_map(|p| self.word(p).map(|(ph, _)| ph))
                .collect();
            if !parts.is_empty() {
                return Some((parts.join(" "), Source::Hyphenated));
            }
        }
        if let Some(p) = self.spell_acronym(w) {
            return Some((p, Source::Spelled));
        }
        if let Some(p) = self.compound(w) {
            return Some((p, Source::Compound));
        }
        let p = letter_to_sound(w);
        if p.is_empty() {
            return None;
        }
        Some((p, Source::Rules))
    }

    /// Derives inflected forms from a dictionary base word.
    fn word_form(&self, w: &str) -> Option<String> {
        let lower = w.to_lowercase();
        let base = |b: &str| -> Option<String> {
            if b.len() < 2 {
                return None;
            }
            self.lex.get(b).map(str::to_string)
        };
        // Possessives: dog's, James'.
        if let Some(b) = lower.strip_suffix("'s") {
            if let Some(p) = base(b).or_else(|| self.word(b).map(|x| x.0)) {
                return Some(suffix_s(&p));
            }
        }
        if let Some(b) = lower.strip_suffix("s'") {
            if let Some(p) = base(b) {
                return Some(suffix_s(&p));
            }
        }
        // -ies / -ied: parties, carried.
        for (suf, add) in [("ies", "z"), ("ied", "d")] {
            if let Some(stem) = lower.strip_suffix(suf) {
                if let Some(p) = base(&format!("{stem}y")) {
                    return Some(add_stress(&format!("{p}{add}")));
                }
            }
        }
        // -es after sibilants: boxes, wishes.
        if let Some(stem) = lower.strip_suffix("es") {
            if ["s", "x", "z", "ch", "sh"]
                .iter()
                .any(|e| stem.ends_with(e))
            {
                if let Some(p) = base(stem) {
                    return Some(suffix_s(&p));
                }
            }
        }
        if let Some(stem) = lower.strip_suffix('s') {
            if !stem.ends_with('s') {
                if let Some(p) = base(stem) {
                    return Some(suffix_s(&p));
                }
            }
        }
        if let Some(stem) = lower.strip_suffix("ed") {
            for b in stems(stem) {
                if let Some(p) = base(&b) {
                    return Some(suffix_ed(&p));
                }
            }
        }
        if let Some(stem) = lower.strip_suffix("ing") {
            for b in stems(stem) {
                if let Some(p) = base(&b) {
                    return Some(add_stress(&format!("{p}ɪŋ")));
                }
            }
        }
        if let Some(stem) = lower.strip_suffix("ly") {
            if let Some(p) = base(stem) {
                return Some(add_stress(&format!("{p}li")));
            }
        }
        if let Some(stem) = lower.strip_suffix("ness") {
            if let Some(p) = base(stem).or_else(|| base(&format!("{}y", stem.strip_suffix('i')?))) {
                return Some(add_stress(&format!("{p}nəs")));
            }
        }
        None
    }

    /// Spells short all-capital words letter by letter ("FBI", "NPR").
    fn spell_acronym(&self, w: &str) -> Option<String> {
        let letters: Vec<char> = w.chars().filter(|c| c.is_alphabetic()).collect();
        let all_caps = letters.iter().all(|c| c.is_uppercase());
        if letters.is_empty() || letters.len() > 5 || !(all_caps || letters.len() == 1) {
            return None;
        }
        let names: Option<Vec<String>> = letters
            .iter()
            .map(|c| {
                self.lex
                    .exact(&c.to_uppercase().to_string())
                    .map(|p| p.replace('ˈ', "ˌ"))
            })
            .collect();
        let mut names = names?;
        // Main stress on the last letter: ˌɛf bˌi ˈI.
        if let Some(last) = names.last_mut() {
            *last = last.replacen('ˌ', "ˈ", 1);
        }
        Some(names.concat())
    }

    /// Splits an unknown word into two dictionary words ("moonbase" -> moon + base).
    fn compound(&self, w: &str) -> Option<String> {
        let lower = w.to_lowercase();
        if lower.len() < 6 || !lower.is_ascii() {
            return None;
        }
        // Prefer the longest first part.
        for i in (3..=lower.len() - 3).rev() {
            let (a, b) = lower.split_at(i);
            if let (Some(pa), Some(pb)) = (self.lex.exact(a), self.lex.exact(b)) {
                let pb = pb.replace('ˈ', "ˌ");
                return Some(format!("{pa}{pb}"));
            }
        }
        None
    }
}

/// Candidate base words for a stem left after removing -ed or -ing: as is, with a final e
/// restored (baked, making), or with a doubled consonant undone (stopped, running).
fn stems(stem: &str) -> Vec<String> {
    let mut v = vec![stem.to_string(), format!("{stem}e")];
    let b = stem.as_bytes();
    if b.len() >= 2 && b[b.len() - 1] == b[b.len() - 2] {
        v.push(stem[..stem.len() - 1].to_string());
    }
    v
}

fn last_phoneme(p: &str) -> Option<char> {
    p.chars().rev().find(|c| !matches!(c, 'ˈ' | 'ˌ' | 'ː'))
}

/// Plural / possessive / third person: cats (s), dogs (z), buses (ᵻz).
fn suffix_s(p: &str) -> String {
    let add = match last_phoneme(p) {
        Some(c) if "szʃʒʧʤ".contains(c) => "ᵻz",
        Some(c) if "ptkfθ".contains(c) => "s",
        _ => "z",
    };
    add_stress(&format!("{p}{add}"))
}

/// Past tense: wanted (ᵻd), walked (t), played (d).
fn suffix_ed(p: &str) -> String {
    let add = match last_phoneme(p) {
        Some(c) if "td".contains(c) => "ᵻd",
        Some(c) if "pkfθʃsʧ".contains(c) => "t",
        _ => "d",
    };
    add_stress(&format!("{p}{add}"))
}

/// Dictionary entries for short words can carry only secondary stress ("mˌAk"); a word on
/// its own needs a primary one.
fn add_stress(p: &str) -> String {
    if p.contains('ˈ') {
        p.to_string()
    } else if p.contains('ˌ') {
        p.replacen('ˌ', "ˈ", 1)
    } else {
        add_primary_stress(p)
    }
}

enum Raw {
    Word(String),
    Punct(char),
}

fn tokenize(text: &str) -> Vec<Raw> {
    let mut out = Vec::new();
    let mut cur = String::new();
    let chars: Vec<char> = text.chars().collect();
    let flush = |cur: &mut String, out: &mut Vec<Raw>| {
        let w = cur.trim_matches(|c| c == '-' || c == '\'').to_string();
        if w.chars().any(char::is_alphabetic) {
            out.push(Raw::Word(w));
        }
        cur.clear();
    };
    for (i, &c) in chars.iter().enumerate() {
        let next_is_letter = chars.get(i + 1).is_some_and(|n| n.is_alphanumeric());
        if c.is_alphanumeric() || ((c == '\'' || c == '-') && !cur.is_empty() && next_is_letter) {
            cur.push(c);
        } else {
            flush(&mut cur, &mut out);
            if PUNCT.contains(c) {
                out.push(Raw::Punct(c));
            } else if c == '-' || c == '–' {
                // A free-standing dash is a pause.
                out.push(Raw::Punct('—'));
            }
        }
    }
    flush(&mut cur, &mut out);
    out
}

/// Joins tokens the way misaki does: words separated by spaces, punctuation attached.
pub fn render(tokens: &[Token]) -> String {
    let mut s = String::new();
    for t in tokens {
        match t {
            Token::Word { phonemes, .. } => {
                if !s.is_empty() && !s.ends_with(['(', '“']) && !s.ends_with(' ') {
                    s.push(' ');
                }
                s.push_str(phonemes);
            }
            Token::Punct(c) if "(“".contains(*c) => {
                if !s.is_empty() {
                    s.push(' ');
                }
                s.push(*c);
            }
            Token::Punct(c) => s.push(*c),
        }
    }
    s
}

impl Phonemizer for EnglishG2p {
    fn phonemize(&self, sentence: &str) -> Result<String, EngineError> {
        Ok(render(&self.analyse(sentence)))
    }
}

/// True if the phoneme string has at least one vowel (used by tests and quality checks).
pub fn has_vowel(p: &str) -> bool {
    p.chars().any(|c| VOWELS.contains(c))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn g2p() -> EnglishG2p {
        EnglishG2p::new(Lexicon::from_pairs(&[
            ("hello", "həlˈO"),
            ("world", "wˈɜɹld"),
            ("cat", "kˈæt"),
            ("dog", "dˈɔɡ"),
            ("bus", "bˈʌs"),
            ("walk", "wˈɔk"),
            ("want", "wˈɑnt"),
            ("play", "plˈA"),
            ("make", "mˌAk"),
            ("stop", "stˈɑp"),
            ("party", "pˈɑɹti"),
            ("quick", "kwˈɪk"),
            ("kind", "kˈInd"),
            ("moon", "mˈun"),
            ("base", "bˈAs"),
            ("forty", "fˈɔɹti"),
            ("two", "tˈu"),
            ("dollars", "dˈɑləɹz"),
            ("well", "wˈɛl"),
            ("known", "nˈOn"),
            ("F", "ˈɛf"),
            ("B", "bˈi"),
            ("I", "ˈI"),
        ]))
    }

    fn sources(s: &str) -> Vec<(String, Source)> {
        g2p()
            .analyse(s)
            .into_iter()
            .filter_map(|t| match t {
                Token::Word { text, source, .. } => Some((text, source)),
                _ => None,
            })
            .collect()
    }

    #[test]
    fn dictionary_words_and_punctuation() {
        assert_eq!(g2p().phonemize("Hello, world!").unwrap(), "həlˈO, wˈɜɹld!");
    }

    #[test]
    fn word_forms() {
        let g = g2p();
        let p = |w: &str| g.word(w).unwrap();
        assert_eq!(p("cats"), ("kˈæts".into(), Source::WordForm));
        assert_eq!(p("dogs").0, "dˈɔɡz");
        assert_eq!(p("buses").0, "bˈʌsᵻz");
        assert_eq!(p("dog's").0, "dˈɔɡz");
        assert_eq!(p("walked").0, "wˈɔkt");
        assert_eq!(p("wanted").0, "wˈɑntᵻd");
        assert_eq!(p("played").0, "plˈAd");
        assert_eq!(p("making").0, "mˈAkɪŋ");
        assert_eq!(p("stopped").0, "stˈɑpt");
        assert_eq!(p("stopping").0, "stˈɑpɪŋ");
        assert_eq!(p("parties").0, "pˈɑɹtiz");
        assert_eq!(p("quickly").0, "kwˈɪkli");
        assert_eq!(p("kindness").0, "kˈIndnəs");
    }

    #[test]
    fn acronyms_compounds_hyphens_and_rules() {
        let s = sources("FBI moonbase well-known Voxa");
        assert_eq!(
            s,
            vec![
                ("FBI".into(), Source::Spelled),
                ("moonbase".into(), Source::Compound),
                ("well-known".into(), Source::Hyphenated),
                ("Voxa".into(), Source::Rules),
            ]
        );
        assert_eq!(g2p().word("FBI").unwrap().0, "ˌɛfbˌiˈI");
    }

    #[test]
    fn numbers_are_spoken_not_dropped() {
        assert_eq!(g2p().phonemize("$42").unwrap(), "fˈɔɹti tˈu dˈɑləɹz");
    }

    #[test]
    fn no_word_is_ever_dropped() {
        let text = "Zxqwv blorptastic teleporters cost 42 dollars on 3/4/2026, \
                    said Dr. O'Brien-Smith (the CEO) — wow… really?!";
        let tokens = g2p().analyse(text);
        let words: Vec<&Token> = tokens
            .iter()
            .filter(|t| matches!(t, Token::Word { .. }))
            .collect();
        let normalized = normalize::normalize(text);
        let expected = normalized
            .split(|c: char| c.is_whitespace() || PUNCT.contains(c))
            .filter(|w| w.chars().any(char::is_alphabetic))
            .count();
        assert_eq!(words.len(), expected, "{normalized}");
        for t in words {
            if let Token::Word { text, phonemes, .. } = t {
                assert!(!phonemes.is_empty(), "{text} has no phonemes");
            }
        }
    }

    #[test]
    fn dashes_and_quotes_render_as_pauses() {
        let out = g2p().phonemize("hello - world (cat)").unwrap();
        assert_eq!(out, "həlˈO— wˈɜɹld (kˈæt)");
    }

    #[test]
    fn non_latin_text_does_not_crash() {
        assert_eq!(g2p().phonemize("日本語").unwrap(), "");
        assert_eq!(g2p().phonemize("🙂🙂").unwrap(), "");
    }
}
