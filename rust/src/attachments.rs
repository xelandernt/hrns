use agent_client_protocol::v1::schema::{
    BlobResourceContents, ContentBlock, EmbeddedResource, EmbeddedResourceResource, Meta,
    TextContent, TextResourceContents,
};
use base64::{Engine, engine::general_purpose::STANDARD};
use percent_encoding::{AsciiSet, CONTROLS, utf8_percent_encode};

pub const MAX_FILE_BYTES: usize = 8 * 1024 * 1024;
pub const MAX_PROMPT_BYTES: usize = 9 * 1024 * 1024;
const URI_PATH: &AsciiSet = &CONTROLS
    .add(b' ')
    .add(b'"')
    .add(b'#')
    .add(b'%')
    .add(b'<')
    .add(b'>')
    .add(b'?')
    .add(b'[')
    .add(b'\\')
    .add(b']')
    .add(b'`');

pub struct Attachment {
    pub name: String,
    pub size: usize,
    block: ContentBlock,
}

impl Attachment {
    pub fn new(
        name: String,
        path: &str,
        media: Option<String>,
        bytes: Vec<u8>,
    ) -> Result<Self, String> {
        let size = bytes.len();
        if size > MAX_FILE_BYTES {
            return Err("Attachment exceeds the 8 MiB file limit".into());
        }
        let mime = media.filter(|kind| !kind.is_empty()).unwrap_or_else(|| {
            mime_guess::from_path(&name)
                .first_or_octet_stream()
                .to_string()
        });
        let uri = file_uri(path);
        let resource = if is_text(&name, &mime) {
            match String::from_utf8(bytes) {
                Ok(text) => EmbeddedResourceResource::TextResourceContents(
                    TextResourceContents::new(text, uri).mime_type(mime),
                ),
                Err(error) => blob(error.as_bytes(), uri, "application/octet-stream".into()),
            }
        } else {
            blob(&bytes, uri, mime)
        };
        Ok(Self {
            name,
            size,
            block: ContentBlock::Resource(EmbeddedResource::new(resource)),
        })
    }
}

pub struct Prompt {
    pub text: String,
    pub attachments: Vec<Attachment>,
    pub skill_meta: Option<serde_json::Value>,
}

impl Prompt {
    pub fn into_blocks(self) -> Vec<ContentBlock> {
        let mut text = TextContent::new(self.text);
        if let Some(payload) = self.skill_meta {
            let mut meta = Meta::new();
            meta.insert(crate::skills::META_KEY.to_owned(), payload);
            text = text.meta(meta);
        }
        std::iter::once(ContentBlock::Text(text))
            .chain(
                self.attachments
                    .into_iter()
                    .map(|attachment| attachment.block),
            )
            .collect()
    }
}

fn blob(bytes: &[u8], uri: String, mime: String) -> EmbeddedResourceResource {
    EmbeddedResourceResource::BlobResourceContents(
        BlobResourceContents::new(STANDARD.encode(bytes), uri).mime_type(mime),
    )
}

fn file_uri(path: &str) -> String {
    let encoded = utf8_percent_encode(path, URI_PATH);
    if path.starts_with('/') {
        format!("file://{encoded}")
    } else {
        format!("file:///{encoded}")
    }
}

fn is_text(name: &str, media: &str) -> bool {
    media.starts_with("text/")
        || matches!(
            media,
            "application/json" | "application/xml" | "application/yaml"
        )
        || name.rsplit_once('.').is_some_and(|(_, suffix)| {
            matches!(
                suffix.to_ascii_lowercase().as_str(),
                "json"
                    | "jsonl"
                    | "yaml"
                    | "yml"
                    | "toml"
                    | "md"
                    | "txt"
                    | "csv"
                    | "tsv"
                    | "py"
                    | "ts"
                    | "tsx"
                    | "js"
                    | "jsx"
                    | "css"
                    | "html"
                    | "xml"
                    | "sql"
                    | "sh"
                    | "rs"
                    | "go"
                    | "java"
                    | "kt"
                    | "c"
                    | "h"
                    | "cpp"
                    | "hpp"
                    | "rb"
                    | "php"
            )
        })
}

#[cfg(not(target_family = "wasm"))]
pub fn read_native(path: &str) -> Result<Attachment, String> {
    use std::fs::File;
    use std::io::Read;
    use std::path::Path;

    let path = Path::new(path)
        .canonicalize()
        .map_err(|error| error.to_string())?;
    let info = path.metadata().map_err(|error| error.to_string())?;
    if !info.is_file() {
        return Err("Only regular files can be attached".into());
    }
    if info.len() > MAX_FILE_BYTES as u64 {
        return Err("Attachment exceeds the 8 MiB file limit".into());
    }
    let name = path
        .file_name()
        .ok_or("Missing attachment filename")?
        .to_string_lossy()
        .into_owned();
    let mut data = Vec::new();
    File::open(&path)
        .map_err(|error| error.to_string())?
        .take(MAX_FILE_BYTES as u64 + 1)
        .read_to_end(&mut data)
        .map_err(|error| error.to_string())?;
    #[cfg(windows)]
    let path = path.to_string_lossy().replace('\\', "/");
    #[cfg(not(windows))]
    let path = path.to_string_lossy();
    Attachment::new(name, &path, None, data)
}
