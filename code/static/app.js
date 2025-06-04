document.addEventListener('DOMContentLoaded', () => {
    const statusDiv = document.getElementById("status");
    const messagesDiv = document.getElementById("messages");
    const speedSlider = document.getElementById("speedSlider");
    speedSlider.disabled = true; // start disabled

    // Create an instance of VoiceChatClient
    const voiceChatClient = new VoiceChatClient(statusDiv, messagesDiv, speedSlider);

    // UI Controls
    document.getElementById("clearBtn").onclick = () => {
        voiceChatClient.clearHistory();
    };

    speedSlider.addEventListener("input", (e) => {
        const speedValue = parseInt(e.target.value);
        voiceChatClient.setSpeed(speedValue);
    });

    document.getElementById("startBtn").onclick = async () => {
        await voiceChatClient.start();
    };

    document.getElementById("stopBtn").onclick = () => {
        voiceChatClient.stop();
    };

    document.getElementById("copyBtn").onclick = () => {
        voiceChatClient.copyConversation();
    };

    // Initial render of messages (usually empty at the start)
    voiceChatClient.renderMessages();
});
