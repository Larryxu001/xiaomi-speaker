//! Cuts a reply into speakable pieces while the model is still writing it.
//!
//! The first piece is cut early (at a comma, once it has a few characters) so
//! the speaker starts talking as soon as possible; later pieces end at full
//! sentence marks unless they grow long.

const HARD: &[char] = &['。', '！', '？', '!', '?', '；', ';', '\n', '…'];
const SOFT: &[char] = &['，', ',', '、', '：', ':'];
const FIRST_MIN_CHARS: usize = 6;
const LONG_CHARS: usize = 40;

#[derive(Default)]
pub struct SentenceSplitter {
    buf: String,
    sent_any: bool,
}

impl SentenceSplitter {
    pub fn push(&mut self, text: &str) -> Vec<String> {
        self.buf.push_str(text);
        let mut out = Vec::new();
        while let Some(end) = self.boundary() {
            let piece: String = self.buf.drain(..end).collect();
            if let Some(piece) = clean(&piece) {
                out.push(piece);
                self.sent_any = true;
            }
        }
        out
    }

    pub fn finish(&mut self) -> Option<String> {
        let rest = std::mem::take(&mut self.buf);
        clean(&rest)
    }

    /// Byte index just past the first place the buffer can be cut.
    fn boundary(&self) -> Option<usize> {
        let mut chars = 0;
        for (i, c) in self.buf.char_indices() {
            chars += 1;
            let soft_ok = (!self.sent_any && chars >= FIRST_MIN_CHARS) || chars >= LONG_CHARS;
            if HARD.contains(&c) || (soft_ok && SOFT.contains(&c)) {
                return Some(i + c.len_utf8());
            }
        }
        None
    }
}

/// Drop Markdown marks the model sometimes adds; None if nothing speakable is left.
fn clean(piece: &str) -> Option<String> {
    let text: String = piece.chars().filter(|c| !matches!(c, '*' | '#' | '`' | '_')).collect();
    let text = text.trim();
    (!text.is_empty() && text.chars().any(|c| c.is_alphanumeric())).then(|| text.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn first_piece_is_cut_early_then_full_sentences() {
        let mut s = SentenceSplitter::default();
        let mut out = s.push("好的，明天上海");
        out.extend(s.push("多云，最高十八度，最低十度。记得带"));
        out.extend(s.push("一件外套。"));
        assert_eq!(out, vec!["好的，明天上海多云，", "最高十八度，最低十度。", "记得带一件外套。"]);
        assert_eq!(s.finish(), None);
    }

    #[test]
    fn leftover_text_is_flushed() {
        let mut s = SentenceSplitter::default();
        assert!(s.push("**已切换**到晓晓").is_empty());
        assert_eq!(s.finish().as_deref(), Some("已切换到晓晓"));
    }
}
