//! Types shared by the native MCP sidecar bridge and the application.
//!
//! Kept separate from `mcp.rs` so the browser build can still match the shared
//! event enum and formally decline MCP requests.
#![cfg_attr(target_family = "wasm", allow(dead_code))]

use agent_client_protocol::v1::schema::{
    McpServer, McpServerAcp, MessageMcpNotification, MessageMcpRequest, MessageMcpResponse,
};
use agent_client_protocol::{JsonRpcNotification, JsonRpcRequest, JsonRpcResponse};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize, JsonRpcRequest)]
#[request(method = "mcp/message", response = AcpMcpResponse)]
#[serde(transparent)]
pub struct AcpMcpRequest(pub MessageMcpRequest);

#[derive(Debug, Clone, Serialize, Deserialize, JsonRpcResponse)]
#[serde(transparent)]
pub struct AcpMcpResponse(pub MessageMcpResponse);

#[derive(Debug, Clone, Serialize, Deserialize, JsonRpcNotification)]
#[notification(method = "mcp/message")]
#[serde(transparent)]
pub struct AcpMcpNotification(pub MessageMcpNotification);

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ConsentDecision {
    Once,
    Session,
    Always,
    Deny,
}

impl ConsentDecision {
    pub fn label(self) -> &'static str {
        match self {
            Self::Once => "once",
            Self::Session => "session",
            Self::Always => "always",
            Self::Deny => "deny",
        }
    }
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Health {
    pub name: String,
    #[serde(default)]
    pub transport: String,
    #[serde(default)]
    pub target: String,
    #[serde(default)]
    pub export: bool,
    #[serde(default)]
    pub state: String,
    #[serde(default)]
    pub detail: String,
    #[serde(default)]
    pub protocol: String,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Declaration {
    pub name: String,
    pub server_id: String,
}

pub fn servers(declarations: &[Declaration]) -> Vec<McpServer> {
    declarations
        .iter()
        .map(|declaration| {
            McpServer::Acp(McpServerAcp::new(
                declaration.name.clone(),
                declaration.server_id.clone(),
            ))
        })
        .collect()
}
