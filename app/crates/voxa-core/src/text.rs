//! Input text: validation and sentence splitting.
//!
//! Engines generate one sentence at a time (as the Voxa server does) so long text gets
//! progress, bounded memory and early playback.

use thiserror::Error;

/// Longest text accepted in one request, in characters. Matches the website's limit; the app
/// splits longer documents (audiobooks) into parts before they reach an engine.
pub const MAX_CHARS: usize = 4000;

#[derive(Debug, Error, PartialEq, Eq)]
pub enum TextError {
    #[error("text is empty")]
    Empty,
    #[error("text is {0} characters; the limit is {MAX_CHARS}")]
    TooLong(usize),
}

/// Trims the text and checks it is non-empty and within [`MAX_CHARS`].
pub fn validate_text(text: &str) -> Result<&str, TextError> {
    let t = text.trim();
    if t.is_empty() {
        return Err(TextError::Empty);
    }
    let n = t.chars().count();
    if n > MAX_CHARS {
        return Err(TextError::TooLong(n));
    }
    Ok(t)
}

/// Sentence-ending characters, including CJK and Devanagari full stops.
fn is_terminal(c: char) -> bool {
    matches!(c, '.' | '!' | '?' | '…' | '。' | '！' | '？' | '।')
}

/// Closing quotes and brackets that belong to the sentence they follow.
fn is_closer(c: char) -> bool {
    matches!(
        c,
        '"' | '\'' | ')' | ']' | '»' | '”' | '’' | '」' | '』' | '）'
    )
}

/// Abbreviations whose period does not end a sentence. Words like "etc." that often do end
/// one are left out on purpose.
const ABBREVIATIONS: &[&str] = &[
    "Mr", "Mrs", "Ms", "Dr", "Prof", "St", "Jr", "Sr", "vs", "Mt", "Ave", "No", "Fig", "e.g",
    "i.e", "approx",
];

/// True if the period at `chars[i]` closes an abbreviation or an initial ("J. R. R.").
fn is_abbreviation(chars: &[char], i: usize) -> bool {
    let start = chars[..i]
        .iter()
        .rposition(|c| c.is_whitespace() || "(\"'“".contains(*c))
        .map_or(0, |p| p + 1);
    let word: String = chars[start..i].iter().collect();
    if word.chars().count() == 1 && word.chars().all(char::is_uppercase) {
        return true;
    }
    // "No." is an abbreviation only before a number ("No. 5").
    if word == "No" {
        let next = chars[i + 1..].iter().find(|c| !c.is_whitespace());
        return next.is_some_and(|c| c.is_ascii_digit());
    }
    ABBREVIATIONS.contains(&word.as_str())
}

/// Characters after which a terminal ends a sentence without needing a space.
fn is_cjk_terminal(c: char) -> bool {
    matches!(c, '。' | '！' | '？')
}

/// Splits text into sentences, keeping punctuation and closing quotes with their sentence.
///
/// A sentence ends at `.`, `!`, `?`, `…` or a full stop from another script, followed by
/// whitespace or the end of the text (CJK full stops need no space), or at a blank line.
/// Runs of terminals ("?!", "...") stay together. Empty pieces are dropped.
pub fn split_sentences(text: &str) -> Vec<String> {
    let chars: Vec<char> = text.chars().collect();
    let mut out = Vec::new();
    let mut cur = String::new();
    let mut i = 0;
    while i < chars.len() {
        let c = chars[i];
        // A blank line ends a sentence even without punctuation (headings, list items).
        if c == '\n' && chars.get(i + 1) == Some(&'\n') {
            push_trimmed(&mut out, &mut cur);
            i += 2;
            continue;
        }
        cur.push(c);
        if is_terminal(c) {
            let cjk = is_cjk_terminal(c);
            let mut j = i + 1;
            while j < chars.len() && (is_terminal(chars[j]) || is_closer(chars[j])) {
                cur.push(chars[j]);
                j += 1;
            }
            let at_end = j >= chars.len();
            let abbreviation = c == '.' && j == i + 1 && is_abbreviation(&chars, i);
            if !abbreviation && (at_end || cjk || chars[j].is_whitespace()) {
                push_trimmed(&mut out, &mut cur);
            }
            i = j;
            continue;
        }
        i += 1;
    }
    push_trimmed(&mut out, &mut cur);
    out
}

fn push_trimmed(out: &mut Vec<String>, cur: &mut String) {
    let s = cur.trim();
    if !s.is_empty() {
        out.push(s.to_string());
    }
    cur.clear();
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn validate_trims_and_checks_bounds() {
        assert_eq!(validate_text("  hi  "), Ok("hi"));
        assert_eq!(validate_text(" \n "), Err(TextError::Empty));
        let long = "a".repeat(MAX_CHARS + 1);
        assert_eq!(validate_text(&long), Err(TextError::TooLong(MAX_CHARS + 1)));
        assert!(validate_text(&"a".repeat(MAX_CHARS)).is_ok());
    }

    #[test]
    fn validate_counts_characters_not_bytes() {
        // 4000 Japanese characters are 12000 bytes but within the limit.
        assert!(validate_text(&"あ".repeat(MAX_CHARS)).is_ok());
    }

    #[test]
    fn splits_simple_sentences() {
        assert_eq!(
            split_sentences("Hello there. How are you? Fine!"),
            vec!["Hello there.", "How are you?", "Fine!"]
        );
    }

    #[test]
    fn keeps_runs_and_closing_quotes() {
        assert_eq!(
            split_sentences("Really?! \"Yes.\" Wait..."),
            vec!["Really?!", "\"Yes.\"", "Wait..."]
        );
    }

    #[test]
    fn does_not_split_inside_numbers_or_words() {
        assert_eq!(
            split_sentences("It costs 3.50 today. version1.2 ships."),
            vec!["It costs 3.50 today.", "version1.2 ships."]
        );
    }

    #[test]
    fn abbreviations_and_initials_do_not_end_sentences() {
        assert_eq!(
            split_sentences("Mr. Smith met Dr. Jones. They talked."),
            vec!["Mr. Smith met Dr. Jones.", "They talked."]
        );
        assert_eq!(
            split_sentences("J. R. R. Tolkien wrote it. Read No. 5 first."),
            vec!["J. R. R. Tolkien wrote it.", "Read No. 5 first."]
        );
        // "No." not before a number, and "etc.", still end sentences.
        assert_eq!(
            split_sentences("I said No. Then left, etc. Bye."),
            vec!["I said No.", "Then left, etc.", "Bye."]
        );
    }

    #[test]
    fn splits_cjk_without_spaces() {
        assert_eq!(
            split_sentences("今日は晴れです。明日は雨です。"),
            vec!["今日は晴れです。", "明日は雨です。"]
        );
    }

    #[test]
    fn blank_line_ends_a_sentence() {
        assert_eq!(
            split_sentences("Chapter One\n\nIt was dark"),
            vec!["Chapter One", "It was dark"]
        );
    }

    #[test]
    fn empty_and_whitespace_give_nothing() {
        assert!(split_sentences("").is_empty());
        assert!(split_sentences("  \n\n  ").is_empty());
    }
}
