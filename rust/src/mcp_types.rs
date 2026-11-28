//! Shared MCP consent and health. Native ACP carriers use the SDK schema.

#[cfg(not(target_family = "wasm"))]
use agent_client_protocol::v1::schema::{
    MessageMcpNotification, MessageMcpRequest, MessageMcpResponse,
};
#[cfg(not(target_family = "wasm"))]
use agent_client_protocol::{JsonRpcNotification, JsonRpcRequest, JsonRpcResponse};
#[cfg(not(target_family = "wasm"))]
use serde::{Deserialize, Serialize};

pub const SIGN_IN_REQUIRED: &str = "Sign-in required";

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

#[cfg(not(target_family = "wasm"))]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum AuthAction {
    SignIn,
    SignOut,
}

#[derive(Clone, Debug, Default)]
pub struct Health {
    pub name: String,
    pub transport: String,
    pub target: String,
    pub state: String,
    pub detail: String,
    pub startup_failure: Option<String>,
    pub protocol: String,
    #[cfg(not(target_family = "wasm"))]
    pub oauth_signed_in: bool,
    pub can_sign_out: bool,
}
