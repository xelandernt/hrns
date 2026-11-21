use std::collections::BTreeMap;
use std::env;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};

use serde::Deserialize;
use toml_edit::{DocumentMut, Item, TableLike};

#[derive(Deserialize, Default)]
struct Settings {
    #[serde(default)]
    mcp: McpSettings,
}

#[derive(Deserialize, Default)]
struct McpSettings {
    #[serde(default)]
    servers: BTreeMap<String, Server>,
}

#[derive(Deserialize)]
#[serde(default)]
pub(super) struct Server {
    pub command: String,
    pub args: Vec<String>,
    pub env: BTreeMap<String, String>,
    pub cwd: String,
    pub url: String,
    pub headers: BTreeMap<String, String>,
    pub enabled: bool,
    pub always_allow: bool,
    pub include_tools: Vec<String>,
    pub exclude_tools: Vec<String>,
    pub timeout: f64,
}

impl Default for Server {
    fn default() -> Self {
        Self {
            command: String::new(),
            args: Vec::new(),
            env: BTreeMap::new(),
            cwd: String::new(),
            url: String::new(),
            headers: BTreeMap::new(),
            enabled: true,
            always_allow: false,
            include_tools: Vec::new(),
            exclude_tools: Vec::new(),
            timeout: 10.0,
        }
    }
}

impl Server {
    pub fn transport(&self) -> &'static str {
        if self.url.is_empty() { "stdio" } else { "http" }
    }

    pub fn target(&self) -> String {
        if self.url.is_empty() {
            self.command.clone()
        } else {
            reqwest::Url::parse(&self.url)
                .ok()
                .and_then(|url| url.host_str().map(str::to_owned))
                .unwrap_or_else(|| "invalid HTTP URL".into())
        }
    }
}

pub(super) fn read(explicit: Option<&Path>) -> io::Result<BTreeMap<String, (Server, PathBuf)>> {
    let paths = if let Some(path) = explicit {
        vec![path.to_owned()]
    } else {
        let mut paths = Vec::new();
        if let Some(dirs) = directories::BaseDirs::new() {
            paths.push(dirs.config_local_dir().join("hrns/config.toml"));
        }
        paths.push(env::current_dir()?.join("hrns.toml"));
        paths
    };
    let mut merged = DocumentMut::new();
    let mut locations = BTreeMap::new();
    for path in paths {
        let text = match fs::read_to_string(&path) {
            Ok(text) => text,
            Err(error) if error.kind() == io::ErrorKind::NotFound => continue,
            Err(error) => return Err(error),
        };
        let document = text.parse::<DocumentMut>().map_err(|_| {
            io::Error::new(
                io::ErrorKind::InvalidData,
                format!("Invalid TOML in {}", path.display()),
            )
        })?;
        if let Some(servers) = server_table(document.as_table()) {
            for (name, _) in servers.iter() {
                locations.insert(name.to_owned(), path.clone());
            }
        }
        merge(merged.as_table_mut(), document.as_table());
    }
    let settings: Settings = toml_edit::de::from_document(merged)
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidData, "Invalid [mcp] configuration"))?;
    settings
        .mcp
        .servers
        .into_iter()
        .map(|(name, server)| {
            let path = locations
                .remove(&name)
                .ok_or_else(|| io::Error::other("Missing MCP server configuration path"))?;
            Ok((name, (server, path)))
        })
        .collect()
}

fn merge(target: &mut dyn TableLike, source: &dyn TableLike) {
    for (key, value) in source.iter() {
        if let Some(old) = target.get_mut(key).and_then(Item::as_table_like_mut)
            && let Some(new) = value.as_table_like()
        {
            merge(old, new);
        } else {
            target.insert(key, value.clone());
        }
    }
}

fn server_table(root: &dyn TableLike) -> Option<&dyn TableLike> {
    root.get("mcp")?
        .as_table_like()?
        .get("servers")?
        .as_table_like()
}

pub(super) fn allow(path: &Path, name: &str) -> io::Result<()> {
    crate::config_file::edit(path, |document| {
        let server = document
            .get_mut("mcp")
            .and_then(Item::as_table_like_mut)
            .and_then(|mcp| mcp.get_mut("servers"))
            .and_then(Item::as_table_like_mut)
            .and_then(|servers| servers.get_mut(name))
            .and_then(Item::as_table_like_mut)
            .ok_or_else(|| io::Error::other("The MCP server configuration no longer exists"))?;
        server.insert("always_allow", toml_edit::value(true));
        Ok(())
    })
}

pub(super) fn expand(value: &str) -> Result<String, String> {
    let mut rest = value;
    let mut output = String::new();
    while let Some((prefix, reference)) = rest.split_once("{env:") {
        output.push_str(prefix);
        let Some((name, suffix)) = reference.split_once('}') else {
            return Err("Unclosed MCP environment reference".into());
        };
        let variable =
            env::var(name).map_err(|_| format!("Environment variable {name} is unavailable"))?;
        output.push_str(&variable);
        rest = suffix;
    }
    output.push_str(rest);
    Ok(output)
}
