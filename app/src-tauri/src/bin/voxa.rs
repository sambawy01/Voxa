//! `voxa`: Voxa from the command line, including `voxa serve` (headless mode).
//! See `voxa_app_lib::headless`.

fn main() {
    match voxa_app_lib::headless::cli(std::env::args().skip(1).collect()) {
        Ok(out) => {
            if !out.is_empty() {
                println!("{out}");
            }
        }
        Err(e) => {
            eprintln!("voxa: {e}");
            std::process::exit(1);
        }
    }
}
