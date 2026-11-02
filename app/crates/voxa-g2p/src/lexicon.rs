//! The pronunciation dictionary: misaki's `us_gold.json` and `us_silver.json` (Apache-2.0,
//! hexgrad/misaki). Gold entries override silver ones.
//!
//! Entries are either a phoneme string or a map of part-of-speech -> phonemes for
//! heteronyms ("read", "live"). Without a part-of-speech tagger Voxa uses `DEFAULT`.

use std::collections::HashMap;

#[derive(Debug, Default)]
pub struct Lexicon {
    map: HashMap<String, String>,
}

impl Lexicon {
    /// Builds a lexicon from misaki JSON files, later files overriding earlier ones (pass
    /// silver then gold).
    pub fn from_json(files: &[&[u8]]) -> Result<Self, serde_json::Error> {
        let mut map = HashMap::new();
        for raw in files {
            let v: serde_json::Map<String, serde_json::Value> = serde_json::from_slice(raw)?;
            for (word, entry) in v {
                let phonemes = match entry {
                    serde_json::Value::String(s) => Some(s),
                    serde_json::Value::Object(o) => o
                        .get("DEFAULT")
                        .and_then(|d| d.as_str())
                        .map(str::to_string),
                    _ => None,
                };
                match phonemes {
                    Some(p) if !p.is_empty() => {
                        map.insert(word, p);
                    }
                    _ => {
                        map.remove(&word);
                    }
                }
            }
        }
        Ok(Self { map })
    }

    /// Small in-memory lexicon for tests.
    pub fn from_pairs(pairs: &[(&str, &str)]) -> Self {
        Self {
            map: pairs
                .iter()
                .map(|(k, v)| (k.to_string(), v.to_string()))
                .collect(),
        }
    }

    pub fn len(&self) -> usize {
        self.map.len()
    }

    pub fn is_empty(&self) -> bool {
        self.map.is_empty()
    }

    /// Exact lookup.
    pub fn exact(&self, word: &str) -> Option<&str> {
        self.map.get(word).map(String::as_str)
    }

    /// Lookup trying the word as written, then lower case, then Title case.
    pub fn get(&self, word: &str) -> Option<&str> {
        self.exact(word)
            .or_else(|| self.exact(&word.to_lowercase()))
            .or_else(|| self.exact(&title_case(word)))
    }
}

pub fn title_case(w: &str) -> String {
    let mut c = w.chars();
    match c.next() {
        Some(f) => f
            .to_uppercase()
            .chain(c.flat_map(char::to_lowercase))
            .collect(),
        None => String::new(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn gold_overrides_silver_and_defaults_heteronyms() {
        let silver = r#"{"paris": "pˈɛɹɪs", "cat": "kˈat"}"#;
        let gold = r#"{"cat": "kˈæt", "read": {"DEFAULT": "ɹˈid", "VBD": "ɹˈɛd"},
                       "us": {"DEFAULT": null}}"#;
        let l = Lexicon::from_json(&[silver.as_bytes(), gold.as_bytes()]).unwrap();
        assert_eq!(l.exact("cat"), Some("kˈæt"));
        assert_eq!(l.exact("read"), Some("ɹˈid"));
        assert_eq!(l.exact("us"), None);
        assert_eq!(l.get("Paris"), Some("pˈɛɹɪs"));
        assert_eq!(l.get("CAT"), Some("kˈæt"));
    }

    #[test]
    fn title_case_works() {
        assert_eq!(title_case("pARIS"), "Paris");
        assert_eq!(title_case(""), "");
    }
}
