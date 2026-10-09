//! Exact JSON reading for the registry.
//!
//! `serde_json`'s default number parser is fast but not correctly rounded: a 17-digit
//! float can come back one ULP away from the value that was written. Its
//! `float_roundtrip` feature fixes that, but Cargo unifies features across the
//! workspace, so enabling it here would change how miner-core parses its own pinned
//! goldens. Instead this module parses JSON text into a [`serde_json::Value`] itself,
//! with every number read by `str::parse` (correctly rounded), and the typed records
//! are then taken from the value with `serde_json::from_value`, which is exact.
//!
//! Integers that fit `u64` or `i64` stay integers, as `serde_json` reads them; any other
//! number becomes an `f64`. Strings accept every JSON escape, surrogate pairs included.

use serde_json::{Map, Number, Value};

/// Why a text is not JSON.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("invalid JSON at byte {at}: {reason}")]
pub struct JsonError {
    /// Byte offset of the problem.
    pub at: usize,
    /// What is wrong.
    pub reason: &'static str,
}

/// Parse one JSON document (surrounding whitespace allowed) with exact numbers.
///
/// # Errors
/// [`JsonError`] for anything that is not exactly one JSON value.
pub fn parse_exact(text: &str) -> Result<Value, JsonError> {
    let mut p = Parser {
        s: text.as_bytes(),
        text,
        i: 0,
        depth: 0,
    };
    let value = p.value()?;
    p.ws();
    if p.i != p.s.len() {
        return Err(p.err("trailing characters"));
    }
    Ok(value)
}

/// Nesting deeper than this is refused rather than recursed into.
const MAX_DEPTH: usize = 512;

struct Parser<'a> {
    s: &'a [u8],
    text: &'a str,
    i: usize,
    depth: usize,
}

impl Parser<'_> {
    fn err(&self, reason: &'static str) -> JsonError {
        JsonError { at: self.i, reason }
    }

    fn ws(&mut self) {
        while let Some(b' ' | b'\t' | b'\n' | b'\r') = self.s.get(self.i) {
            self.i += 1;
        }
    }

    fn peek(&self) -> Option<u8> {
        self.s.get(self.i).copied()
    }

    fn expect(&mut self, byte: u8, reason: &'static str) -> Result<(), JsonError> {
        if self.peek() == Some(byte) {
            self.i += 1;
            Ok(())
        } else {
            Err(self.err(reason))
        }
    }

    fn literal(&mut self, word: &str, value: Value) -> Result<Value, JsonError> {
        if self.s[self.i..].starts_with(word.as_bytes()) {
            self.i += word.len();
            Ok(value)
        } else {
            Err(self.err("unknown literal"))
        }
    }

    fn value(&mut self) -> Result<Value, JsonError> {
        self.ws();
        match self.peek() {
            Some(b'{') => self.nested(Self::object),
            Some(b'[') => self.nested(Self::array),
            Some(b'"') => Ok(Value::String(self.string()?)),
            Some(b't') => self.literal("true", Value::Bool(true)),
            Some(b'f') => self.literal("false", Value::Bool(false)),
            Some(b'n') => self.literal("null", Value::Null),
            Some(b'-' | b'0'..=b'9') => self.number(),
            Some(_) => Err(self.err("unexpected character")),
            None => Err(self.err("unexpected end of input")),
        }
    }

    fn nested(&mut self, f: fn(&mut Self) -> Result<Value, JsonError>) -> Result<Value, JsonError> {
        self.depth += 1;
        if self.depth > MAX_DEPTH {
            return Err(self.err("nested too deeply"));
        }
        let v = f(self);
        self.depth -= 1;
        v
    }

    fn object(&mut self) -> Result<Value, JsonError> {
        self.i += 1; // '{'
        let mut map = Map::new();
        self.ws();
        if self.peek() == Some(b'}') {
            self.i += 1;
            return Ok(Value::Object(map));
        }
        loop {
            self.ws();
            if self.peek() != Some(b'"') {
                return Err(self.err("expected a string key"));
            }
            let key = self.string()?;
            self.ws();
            self.expect(b':', "expected ':'")?;
            let value = self.value()?;
            map.insert(key, value);
            self.ws();
            match self.peek() {
                Some(b',') => self.i += 1,
                Some(b'}') => {
                    self.i += 1;
                    return Ok(Value::Object(map));
                }
                _ => return Err(self.err("expected ',' or '}'")),
            }
        }
    }

    fn array(&mut self) -> Result<Value, JsonError> {
        self.i += 1; // '['
        let mut items = Vec::new();
        self.ws();
        if self.peek() == Some(b']') {
            self.i += 1;
            return Ok(Value::Array(items));
        }
        loop {
            items.push(self.value()?);
            self.ws();
            match self.peek() {
                Some(b',') => self.i += 1,
                Some(b']') => {
                    self.i += 1;
                    return Ok(Value::Array(items));
                }
                _ => return Err(self.err("expected ',' or ']'")),
            }
        }
    }

    fn hex4(&mut self) -> Result<u32, JsonError> {
        let digits = self
            .text
            .get(self.i..self.i + 4)
            .filter(|d| d.bytes().all(|b| b.is_ascii_hexdigit()))
            .ok_or_else(|| self.err("expected four hex digits"))?;
        self.i += 4;
        u32::from_str_radix(digits, 16).map_err(|_| self.err("expected four hex digits"))
    }

    fn string(&mut self) -> Result<String, JsonError> {
        self.i += 1; // '"'
        let mut out = String::new();
        loop {
            let start = self.i;
            while let Some(b) = self.peek() {
                if b == b'"' || b == b'\\' || b < 0x20 {
                    break;
                }
                self.i += 1;
            }
            // The scan stops only at ASCII bytes, so this slice is on char boundaries.
            out.push_str(&self.text[start..self.i]);
            match self.peek() {
                Some(b'"') => {
                    self.i += 1;
                    return Ok(out);
                }
                Some(b'\\') => {
                    self.i += 1;
                    let escape = self.peek().ok_or_else(|| self.err("unfinished escape"))?;
                    self.i += 1;
                    match escape {
                        b'"' => out.push('"'),
                        b'\\' => out.push('\\'),
                        b'/' => out.push('/'),
                        b'b' => out.push('\u{8}'),
                        b'f' => out.push('\u{c}'),
                        b'n' => out.push('\n'),
                        b'r' => out.push('\r'),
                        b't' => out.push('\t'),
                        b'u' => {
                            let mut code = self.hex4()?;
                            if (0xD800..0xDC00).contains(&code) {
                                if !self.s[self.i..].starts_with(b"\\u") {
                                    return Err(self.err("unpaired surrogate"));
                                }
                                self.i += 2;
                                let low = self.hex4()?;
                                if !(0xDC00..0xE000).contains(&low) {
                                    return Err(self.err("unpaired surrogate"));
                                }
                                code = 0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00);
                            }
                            out.push(
                                char::from_u32(code)
                                    .ok_or_else(|| self.err("invalid code point"))?,
                            );
                        }
                        _ => return Err(self.err("unknown escape")),
                    }
                }
                Some(_) => return Err(self.err("control character in a string")),
                None => return Err(self.err("unterminated string")),
            }
        }
    }

    fn digits(&mut self) -> usize {
        let start = self.i;
        while let Some(b'0'..=b'9') = self.peek() {
            self.i += 1;
        }
        self.i - start
    }

    fn number(&mut self) -> Result<Value, JsonError> {
        let start = self.i;
        if self.peek() == Some(b'-') {
            self.i += 1;
        }
        match self.peek() {
            Some(b'0') => self.i += 1,
            Some(b'1'..=b'9') => {
                self.digits();
            }
            _ => return Err(self.err("expected a digit")),
        }
        let mut integer = true;
        if self.peek() == Some(b'.') {
            self.i += 1;
            integer = false;
            if self.digits() == 0 {
                return Err(self.err("expected a digit after '.'"));
            }
        }
        if let Some(b'e' | b'E') = self.peek() {
            self.i += 1;
            integer = false;
            if let Some(b'+' | b'-') = self.peek() {
                self.i += 1;
            }
            if self.digits() == 0 {
                return Err(self.err("expected an exponent"));
            }
        }
        let token = &self.text[start..self.i];
        if integer {
            if let Ok(u) = token.parse::<u64>() {
                return Ok(Value::Number(u.into()));
            }
            if let Ok(i) = token.parse::<i64>() {
                return Ok(Value::Number(i.into()));
            }
        }
        let f: f64 = token.parse().map_err(|_| self.err("invalid number"))?;
        Number::from_f64(f)
            .map(Value::Number)
            .ok_or_else(|| self.err("number out of range"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;

    #[test]
    fn reads_what_serde_json_writes() {
        let v = serde_json::json!({
            "a": [1, -2, 3.5, -0.0, 1e300, 18_446_744_073_709_551_615_u64, -9_223_372_036_854_775_808_i64],
            "s": "q\"\\/\u{8}\u{c}\n\r\t\u{1} é ∑ 😀",
            "t": true, "f": false, "n": null, "o": {}, "e": []
        });
        let text = serde_json::to_string(&v).unwrap();
        assert_eq!(parse_exact(&text).unwrap(), v);
        assert_eq!(
            parse_exact(" [ 1 , 2 ] \n").unwrap(),
            serde_json::json!([1, 2])
        );
        assert_eq!(parse_exact(r#""\ud83d\ude00""#).unwrap(), Value::from("😀"));
    }

    #[test]
    fn refuses_what_is_not_json() {
        for bad in [
            "",
            "{",
            "[1,]",
            "{\"a\" 1}",
            "01",
            "1.",
            "1e",
            "-",
            "tru",
            "\"\\x\"",
            "\"\\ud800\"",
            "1 2",
            "\"a\nb\"",
            "{1: 2}",
        ] {
            assert!(parse_exact(bad).is_err(), "{bad:?}");
        }
    }

    #[test]
    fn a_float_serde_json_misreads_by_default_comes_back_exactly() {
        // serde_json's default parser reads this as ...373 (one ULP off).
        let x = -7.361_753_048_608_373_5_f64;
        let text = serde_json::to_string(&x).unwrap();
        assert_eq!(
            parse_exact(&text).unwrap().as_f64().unwrap().to_bits(),
            x.to_bits()
        );
    }

    proptest! {
        #[test]
        fn every_finite_float_round_trips_bit_for_bit(bits in any::<u64>()) {
            let x = f64::from_bits(bits);
            prop_assume!(x.is_finite());
            let text = serde_json::to_string(&x).unwrap();
            let back = parse_exact(&text).unwrap().as_f64().unwrap();
            // -0.0 is written as "-0.0" and read back as -0.0.
            prop_assert_eq!(back.to_bits(), x.to_bits());
        }
    }
}
