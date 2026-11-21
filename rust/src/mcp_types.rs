//! Shared MCP consent and health. Native ACP carriers use the SDK schema.

#[cfg(not(target_family = "wasm"))]
use agent_client_protocol::v1::schema::{
    MessageMcpNotification, MessageMcpRequest, MessageMcpResponse,
};
#[cfg(not(target_family = "wasm"))]
use agent_client_protocol::{JsonRpcNotification, JsonRpcRequest, JsonRpcResponse};
#[cfg(not(target_family = "wasm"))]
use serde::{Deserialize, Serialize};

#[cfg(not(target_family = "wasm"))]
#[derive(Debug, Clone, Serialize, Deserialize, JsonRpcRequest)]
#[request(method = "mcp/message", response = AcpMcpResponse)]
#[serde(transparent)]
pub struct AcpMcpRequest(pub MessageMcpRequest);

#[cfg(not(target_family = "wasm"))]
#[derive(Debug, Clone, Serialize, Deserialize, JsonRpcResponse)]
#[serde(transparent)]
pub struct AcpMcpResponse(pub MessageMcpResponse);

#[cfg(not(target_family = "wasm"))]
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

#[derive(Clone, Debug, Default)]
pub struct Health {
    pub name: String,
    pub transport: String,
    pub target: String,
    pub state: String,
    pub detail: String,
    pub protocol: String,
}
