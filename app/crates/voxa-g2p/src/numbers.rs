//! Numbers to English words (US conventions).

const ONES: [&str; 20] = [
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
];
const TENS: [&str; 10] = [
    "", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety",
];
const SCALES: [(u64, &str); 5] = [
    (1_000_000_000_000_000, "quadrillion"),
    (1_000_000_000_000, "trillion"),
    (1_000_000_000, "billion"),
    (1_000_000, "million"),
    (1_000, "thousand"),
];

fn below_thousand(n: u64, out: &mut Vec<String>) {
    debug_assert!(n < 1000);
    if n >= 100 {
        out.push(ONES[(n / 100) as usize].into());
        out.push("hundred".into());
    }
    let r = n % 100;
    if r == 0 {
        return;
    }
    if r < 20 {
        out.push(ONES[r as usize].into());
    } else {
        out.push(TENS[(r / 10) as usize].into());
        if !r.is_multiple_of(10) {
            out.push(ONES[(r % 10) as usize].into());
        }
    }
}

/// Cardinal words, e.g. 1234 -> "one thousand two hundred thirty four".
/// Words are space-separated (no hyphens) so each is a dictionary word.
pub fn cardinal(n: u64) -> String {
    if n == 0 {
        return "zero".into();
    }
    let mut out = Vec::new();
    let mut rest = n;
    for (value, name) in SCALES {
        if rest >= value {
            below_thousand(rest / value, &mut out);
            out.push(name.into());
            rest %= value;
        }
    }
    if rest > 0 {
        below_thousand(rest, &mut out);
    }
    out.join(" ")
}

/// Ordinal words, e.g. 21 -> "twenty first", 12 -> "twelfth".
pub fn ordinal(n: u64) -> String {
    let words = cardinal(n);
    let (head, last) = match words.rsplit_once(' ') {
        Some((h, l)) => (format!("{h} "), l.to_string()),
        None => (String::new(), words.clone()),
    };
    let last = match last.as_str() {
        "one" => "first".to_string(),
        "two" => "second".into(),
        "three" => "third".into(),
        "five" => "fifth".into(),
        "eight" => "eighth".into(),
        "nine" => "ninth".into(),
        "twelve" => "twelfth".into(),
        w if w.ends_with('y') => format!("{}ieth", &w[..w.len() - 1]),
        w => format!("{w}th"),
    };
    format!("{head}{last}")
}

/// A four-digit number read as a year, e.g. 1999 -> "nineteen ninety nine",
/// 2005 -> "two thousand five", 2026 -> "twenty twenty six", 1905 -> "nineteen oh five".
pub fn year(n: u64) -> String {
    if !(1000..=9999).contains(&n) {
        return cardinal(n);
    }
    let (hi, lo) = (n / 100, n % 100);
    if (2000..2010).contains(&n) || n.is_multiple_of(1000) {
        return cardinal(n);
    }
    let first = cardinal(hi);
    match lo {
        0 => format!("{first} hundred"),
        1..=9 => format!("{first} oh {}", ONES[lo as usize]),
        _ => format!("{first} {}", cardinal(lo)),
    }
}

/// Digits read one by one, for decimals: "14" -> "one four".
pub fn digits(s: &str) -> String {
    s.chars()
        .filter_map(|c| c.to_digit(10))
        .map(|d| ONES[d as usize])
        .collect::<Vec<_>>()
        .join(" ")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cardinals() {
        assert_eq!(cardinal(0), "zero");
        assert_eq!(cardinal(7), "seven");
        assert_eq!(cardinal(13), "thirteen");
        assert_eq!(cardinal(40), "forty");
        assert_eq!(cardinal(42), "forty two");
        assert_eq!(cardinal(100), "one hundred");
        assert_eq!(cardinal(105), "one hundred five");
        assert_eq!(cardinal(1234), "one thousand two hundred thirty four");
        assert_eq!(cardinal(1_000_000), "one million");
        assert_eq!(
            cardinal(2_500_000_017),
            "two billion five hundred million seventeen"
        );
    }

    #[test]
    fn ordinals() {
        assert_eq!(ordinal(1), "first");
        assert_eq!(ordinal(2), "second");
        assert_eq!(ordinal(3), "third");
        assert_eq!(ordinal(4), "fourth");
        assert_eq!(ordinal(12), "twelfth");
        assert_eq!(ordinal(20), "twentieth");
        assert_eq!(ordinal(21), "twenty first");
        assert_eq!(ordinal(100), "one hundredth");
        assert_eq!(ordinal(108), "one hundred eighth");
    }

    #[test]
    fn years() {
        assert_eq!(year(1999), "nineteen ninety nine");
        assert_eq!(year(1900), "nineteen hundred");
        assert_eq!(year(1905), "nineteen oh five");
        assert_eq!(year(2000), "two thousand");
        assert_eq!(year(2005), "two thousand five");
        assert_eq!(year(2026), "twenty twenty six");
        assert_eq!(year(1066), "ten sixty six");
    }

    #[test]
    fn digit_strings() {
        assert_eq!(digits("1405"), "one four zero five");
    }
}
