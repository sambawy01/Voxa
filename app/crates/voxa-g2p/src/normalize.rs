//! Text normalisation: turns numbers, money, dates, times, symbols and abbreviations into
//! plain words before dictionary lookup (US English conventions).

use std::sync::LazyLock;

use regex::{Captures, Regex};

use crate::numbers::{cardinal, digits, ordinal, year};

fn re(p: &str) -> Regex {
    Regex::new(p).expect("valid regex")
}

const MONTHS: [&str; 12] = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
];

/// Abbreviations that end in a period, with their spoken form. Matched case-sensitively
/// and only before whitespace or the end, so "Dr." expands but "dr" in a word does not.
const ABBREVIATIONS: &[(&str, &str)] = &[
    ("Mr.", "Mister"),
    ("Mrs.", "Missus"),
    ("Ms.", "Miz"),
    ("Dr.", "Doctor"),
    ("Prof.", "Professor"),
    ("St.", "Saint"),
    ("Jr.", "Junior"),
    ("Sr.", "Senior"),
    ("vs.", "versus"),
    ("etc.", "et cetera"),
    ("e.g.", "for example"),
    ("i.e.", "that is"),
    ("approx.", "approximately"),
    ("Mt.", "Mount"),
    ("Ave.", "Avenue"),
    ("Inc.", "Incorporated"),
    ("Ltd.", "Limited"),
    ("Co.", "Company"),
];

/// Currency symbol -> (singular, plural, cents singular, cents plural).
fn currency(sym: &str) -> (&'static str, &'static str, &'static str, &'static str) {
    match sym {
        "£" => ("pound", "pounds", "penny", "pence"),
        "€" => ("euro", "euros", "cent", "cents"),
        "¥" => ("yen", "yen", "", ""),
        _ => ("dollar", "dollars", "cent", "cents"),
    }
}

fn num(s: &str) -> u64 {
    s.replace(',', "").parse().unwrap_or(0)
}

/// Reads a whole-number string: long digit runs (codes, phone numbers) digit by digit.
fn integer(s: &str) -> String {
    let clean = s.replace(',', "");
    if clean.len() > 15 || (clean.len() > 1 && clean.starts_with('0')) {
        return digits(&clean);
    }
    cardinal(num(&clean))
}

fn money(c: &Captures) -> String {
    let (one, many, cent, cents) = currency(&c[1]);
    let whole = num(&c[2]);
    let frac = c.get(3).map(|m| m.as_str());
    if let Some(scale) = c.get(4) {
        // "$2.5 million" -> "two point five million dollars"
        let n = match frac {
            Some(f) => format!("{} point {}", cardinal(whole), digits(f)),
            None => cardinal(whole),
        };
        return format!("{n} {} {many}", scale.as_str());
    }
    let mut parts = Vec::new();
    if whole > 0 || frac.is_none() {
        parts.push(format!(
            "{} {}",
            cardinal(whole),
            if whole == 1 { one } else { many }
        ));
    }
    if let Some(f) = frac {
        let mut c = f.parse::<u64>().unwrap_or(0);
        if f.len() == 1 {
            c *= 10;
        }
        if c > 0 && !cent.is_empty() {
            parts.push(format!(
                "{} {}",
                cardinal(c),
                if c == 1 { cent } else { cents }
            ));
        }
    }
    if parts.is_empty() {
        format!("zero {many}")
    } else {
        parts.join(" and ")
    }
}

fn decimal(int: &str, frac: &str) -> String {
    format!("{} point {}", integer(int), digits(frac))
}

/// Plural of a number's last word: "ninety" -> "nineties", "six" -> "sixes".
fn plural_last(words: &str) -> String {
    match words.rsplit_once(' ') {
        Some((h, l)) => format!("{h} {}", plural_word(l)),
        None => plural_word(words),
    }
}

fn plural_word(w: &str) -> String {
    if let Some(stem) = w.strip_suffix('y') {
        format!("{stem}ies")
    } else if w.ends_with('x') || w.ends_with('s') {
        format!("{w}es")
    } else {
        format!("{w}s")
    }
}

static MONEY: LazyLock<Regex> = LazyLock::new(|| {
    re(
        r"([$£€¥])\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?(?:\s?(thousand|million|billion|trillion)\b)?",
    )
});
static PERCENT: LazyLock<Regex> = LazyLock::new(|| re(r"(\d+(?:\.\d+)?)\s?%"));
static DATE: LazyLock<Regex> = LazyLock::new(|| re(r"\b(\d{1,2})/(\d{1,2})/(\d{4}|\d{2})\b"));
static TIME: LazyLock<Regex> =
    LazyLock::new(|| re(r"\b(\d{1,2}):(\d{2})(?:\s?([AaPp])\.?[Mm]\b\.?)?"));
static ORDINAL: LazyLock<Regex> = LazyLock::new(|| re(r"\b(\d+)(st|nd|rd|th)\b"));
static DECADE: LazyLock<Regex> = LazyLock::new(|| re(r"\b(1[1-9]\d0|20\d0)'?s\b"));
static SHORT_DECADE: LazyLock<Regex> = LazyLock::new(|| re(r"(^|\s)'?([1-9]0)'?s\b"));
static HOUR_AMPM: LazyLock<Regex> = LazyLock::new(|| re(r"(?i)\b(\d{1,2})\s?([ap])\.?m\b\.?"));
static RANGE: LazyLock<Regex> = LazyLock::new(|| re(r"(\d)\s?[–-]\s?(\d)"));
static NEGATIVE: LazyLock<Regex> = LazyLock::new(|| re(r"(^|[\s(])[-−](\d)"));
static GROUPED: LazyLock<Regex> = LazyLock::new(|| re(r"\b\d{1,3}(?:,\d{3})+\b"));
static DECIMAL: LazyLock<Regex> = LazyLock::new(|| re(r"\b(\d+)\.(\d+)\b"));
static YEAR: LazyLock<Regex> = LazyLock::new(|| re(r"\b(1[1-9]\d\d|20\d\d)\b"));
static INTEGER: LazyLock<Regex> = LazyLock::new(|| re(r"\d+"));
static LETTER_DIGIT: LazyLock<Regex> = LazyLock::new(|| re(r"([A-Za-z])(\d)|(\d)([A-Za-z])"));

/// Normalises one sentence (or any text) to words and punctuation.
pub fn normalize(text: &str) -> String {
    let mut s = text
        .replace(['’', '‘'], "'")
        .replace('\u{a0}', " ")
        .replace("...", "…");
    for (abbr, full) in ABBREVIATIONS {
        s = replace_word(&s, abbr, full);
    }
    s = MONEY.replace_all(&s, |c: &Captures| money(c)).into_owned();
    s = PERCENT
        .replace_all(&s, |c: &Captures| {
            let n = &c[1];
            let words = match n.split_once('.') {
                Some((i, f)) => decimal(i, f),
                None => integer(n),
            };
            format!("{words} percent")
        })
        .into_owned();
    s = DATE
        .replace_all(&s, |c: &Captures| {
            let (m, d) = (num(&c[1]), num(&c[2]));
            if !(1..=12).contains(&m) || !(1..=31).contains(&d) {
                return c[0].to_string();
            }
            let y = num(&c[3]);
            let y = if c[3].len() == 2 { 2000 + y } else { y };
            format!("{} {}, {}", MONTHS[m as usize - 1], ordinal(d), year(y))
        })
        .into_owned();
    s = TIME
        .replace_all(&s, |c: &Captures| {
            let (h, m) = (num(&c[1]), num(&c[2]));
            if h > 23 || m > 59 {
                return c[0].to_string();
            }
            let mins = match m {
                0 if c.get(3).is_none() => "o'clock".to_string(),
                0 => String::new(),
                1..=9 => format!("oh {}", cardinal(m)),
                _ => cardinal(m),
            };
            let ampm = match c.get(3).map(|x| x.as_str().to_ascii_lowercase()) {
                Some(a) if a == "a" => " A M",
                Some(_) => " P M",
                None => "",
            };
            format!("{} {mins}{ampm}", cardinal(h))
                .replace("  ", " ")
                .trim()
                .to_string()
        })
        .into_owned();
    s = ORDINAL
        .replace_all(&s, |c: &Captures| ordinal(num(&c[1])))
        .into_owned();
    s = DECADE
        .replace_all(&s, |c: &Captures| plural_last(&year(num(&c[1]))))
        .into_owned();
    s = SHORT_DECADE
        .replace_all(&s, |c: &Captures| {
            format!("{}{}", &c[1], plural_last(&cardinal(num(&c[2]))))
        })
        .into_owned();
    s = HOUR_AMPM
        .replace_all(&s, |c: &Captures| {
            let h = num(&c[1]);
            if h == 0 || h > 12 {
                return c[0].to_string();
            }
            let ap = if c[2].eq_ignore_ascii_case("a") {
                "A"
            } else {
                "P"
            };
            format!("{} {ap} M", cardinal(h))
        })
        .into_owned();
    s = RANGE.replace_all(&s, "$1 to $2").into_owned();
    s = NEGATIVE.replace_all(&s, "${1}minus $2").into_owned();
    s = DECIMAL
        .replace_all(&s, |c: &Captures| decimal(&c[1], &c[2]))
        .into_owned();
    s = GROUPED
        .replace_all(&s, |c: &Captures| integer(&c[0]))
        .into_owned();
    s = YEAR
        .replace_all(&s, |c: &Captures| year(num(&c[1])))
        .into_owned();
    s = LETTER_DIGIT.replace_all(&s, "$1$3 $2$4").into_owned();
    s = LETTER_DIGIT.replace_all(&s, "$1$3 $2$4").into_owned();
    s = INTEGER
        .replace_all(&s, |c: &Captures| integer(&c[0]))
        .into_owned();
    s = s
        .replace('&', " and ")
        .replace('+', " plus ")
        .replace('=', " equals ")
        .replace('@', " at ")
        .replace('°', " degrees ")
        .replace('#', " number ");
    s.split_whitespace().collect::<Vec<_>>().join(" ")
}

/// Replaces `from` when it stands as a whole token (preceded by start/space/punctuation,
/// followed by whitespace or the end).
fn replace_word(s: &str, from: &str, to: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut rest = s;
    while let Some(i) = rest.find(from) {
        let before_ok = rest[..i]
            .chars()
            .last()
            .is_none_or(|c| c.is_whitespace() || "(\"'".contains(c));
        let after = &rest[i + from.len()..];
        let after_ok = after.chars().next().is_none_or(|c| c.is_whitespace());
        let prefix_ok = i > 0 || out.is_empty() || out.ends_with(char::is_whitespace);
        out.push_str(&rest[..i]);
        if before_ok && after_ok && prefix_ok {
            out.push_str(to);
        } else {
            out.push_str(from);
        }
        rest = after;
    }
    out.push_str(rest);
    out
}

#[cfg(test)]
mod tests {
    use super::normalize as n;

    #[test]
    fn plain_numbers() {
        assert_eq!(n("I have 42 cats."), "I have forty two cats.");
        assert_eq!(
            n("1,234 people"),
            "one thousand two hundred thirty four people"
        );
        assert_eq!(n("pi is 3.14"), "pi is three point one four");
        assert_eq!(n("code 007"), "code zero zero seven");
        assert_eq!(n("it was -5 outside"), "it was minus five outside");
    }

    #[test]
    fn money() {
        assert_eq!(n("$5"), "five dollars");
        assert_eq!(n("$1"), "one dollar");
        assert_eq!(n("$12.50"), "twelve dollars and fifty cents");
        assert_eq!(n("$0.99"), "ninety nine cents");
        assert_eq!(n("$1.01"), "one dollar and one cent");
        assert_eq!(n("£3"), "three pounds");
        assert_eq!(n("€2.5 million"), "two point five million euros");
        assert_eq!(n("$1,200"), "one thousand two hundred dollars");
    }

    #[test]
    fn percent_ordinal_years_decades() {
        assert_eq!(n("50% off"), "fifty percent off");
        assert_eq!(n("2.5%"), "two point five percent");
        assert_eq!(n("the 21st century"), "the twenty first century");
        assert_eq!(
            n("in 1999 and 2026"),
            "in nineteen ninety nine and twenty twenty six"
        );
        assert_eq!(n("the 1990s"), "the nineteen nineties");
        assert_eq!(n("the 80s"), "the eighties");
        assert_eq!(n("'90s kids"), "nineties kids");
    }

    #[test]
    fn dates_and_times() {
        assert_eq!(n("on 3/4/2026"), "on March fourth, twenty twenty six");
        assert_eq!(n("at 3:30"), "at three thirty");
        assert_eq!(n("at 3:00"), "at three o'clock");
        assert_eq!(n("at 7:05 pm"), "at seven oh five P M");
        assert_eq!(n("at 9am"), "at nine A M");
        assert_eq!(n("at 11 P.M."), "at eleven P M");
        assert_eq!(n("13/40/2020"), "thirteen/forty/twenty twenty");
    }

    #[test]
    fn ranges_symbols_and_mixed() {
        assert_eq!(n("pages 10-20"), "pages ten to twenty");
        assert_eq!(n("R&D"), "R and D");
        assert_eq!(n("2+2=4"), "two plus two equals four");
        assert_eq!(n("COVID19"), "COVID nineteen");
        assert_eq!(n("90°"), "ninety degrees");
    }

    #[test]
    fn abbreviations_only_as_whole_tokens() {
        assert_eq!(n("Mr. Smith met Dr. Who"), "Mister Smith met Doctor Who");
        assert_eq!(n("apples, pears, etc."), "apples, pears, et cetera");
        assert_eq!(n("Hamstr. is not"), "Hamstr. is not");
        assert_eq!(n("e.g. this"), "for example this");
    }

    #[test]
    fn leaves_words_alone() {
        assert_eq!(n("Hello, world!"), "Hello, world!");
        assert_eq!(n("wait..."), "wait…");
    }
}
