class VoiceChatClient {
    constructor(statusDiv, messagesDiv, speedSlider) {
        this.statusDiv = statusDiv;
        this.messagesDiv = messagesDiv;
        this.speedSlider = speedSlider;

        this.socket = null;
        this.audioContext = null;
        this.mediaStream = null;
        this.micWorkletNode = null;
        this.ttsWorkletNode = null;

        this.isTTSPlaying = false;
        this.ignoreIncomingTTS = false;

        this.chatHistory = [];
        this.typingUser = "";
        this.typingAssistant = "";

        // Batching and header setup
        this.BATCH_SAMPLES = 2048;
        this.HEADER_BYTES = 8;
        this.FRAME_BYTES = this.BATCH_SAMPLES * 2;
        this.MESSAGE_BYTES = this.HEADER_BYTES + this.FRAME_BYTES;

        this.bufferPool = [];
        this.batchBuffer = null;
        this.batchView = null;
        this.batchInt16 = null;
        this.batchOffset = 0;

        this.canSendAudio = false; // New flag to control audio sending
        this.audioBufferQueue = []; // Queue to buffer audio before allowed to send

        this._initializeConsoleLog();
    }

    _initializeConsoleLog() {
        const originalLog = console.log.bind(console);
        console.log = (...args) => {
            const now = new Date();
            const hh = String(now.getHours()).padStart(2, '0');
            const mm = String(now.getMinutes()).padStart(2, '0');
            const ss = String(now.getSeconds()).padStart(2, '0');
            const ms = String(now.getMilliseconds()).padStart(3, '0');
            originalLog(
                `[${hh}:${mm}:${ss}.${ms}]`,
                ...args
            );
        };
    }

    initBatch() {
        if (!this.batchBuffer) {
            this.batchBuffer = this.bufferPool.pop() || new ArrayBuffer(this.MESSAGE_BYTES);
            this.batchView = new DataView(this.batchBuffer);
            this.batchInt16 = new Int16Array(this.batchBuffer, this.HEADER_BYTES);
            this.batchOffset = 0;
        }
    }

    flushBatch() {
        const ts = Date.now() & 0xFFFFFFFF;
        this.batchView.setUint32(0, ts, false);
        const flags = this.isTTSPlaying ? 1 : 0;
        this.batchView.setUint32(4, flags, false);

        // Only send if allowed or send an empty packet if not (to keep connection alive if necessary)
        // However, the goal is to not send audio until signaled.
        // So, we buffer it instead of sending immediately if canSendAudio is false.
        if (this.canSendAudio) {
            this.socket.send(this.batchBuffer);
        } else {
            // Instead of sending, add a copy of the buffer to our queue
            // We need to copy it because batchBuffer will be reused.
            const bufferCopy = this.batchBuffer.slice(0);
            this.audioBufferQueue.push(bufferCopy);
            console.log(`Audio batch buffered. Queue size: ${this.audioBufferQueue.length}`);
        }

        this.bufferPool.push(this.batchBuffer); // Return the original buffer to the pool
        this.batchBuffer = null;
    }

    // This method will be called when the server signals to start sending.
    _sendBufferedAudio() {
        console.log(`Starting to send ${this.audioBufferQueue.length} buffered audio packets.`);
        while(this.audioBufferQueue.length > 0) {
            const buffer = this.audioBufferQueue.shift();
            this.socket.send(buffer);
            console.log(`Sent one buffered audio packet. Remaining: ${this.audioBufferQueue.length}`);
        }
        console.log("Finished sending buffered audio.");
    }

    flushRemainder() {
        if (this.batchOffset > 0) {
            for (let i = this.batchOffset; i < this.BATCH_SAMPLES; i++) {
                this.batchInt16[i] = 0;
            }
            this.flushBatch();
        }
    }

    initAudioContext() {
        if (!this.audioContext) {
            this.audioContext = new AudioContext();
        }
    }

    base64ToInt16Array(b64) {
        const raw = atob(b64);
        const buf = new ArrayBuffer(raw.length);
        const view = new Uint8Array(buf);
        for (let i = 0; i < raw.length; i++) {
            view[i] = raw.charCodeAt(i);
        }
        return new Int16Array(buf);
    }

    async startRawPcmCapture() {
        try {
            const stream = await navigator.mediaDevices.getUserMedia({
                audio: {
                    sampleRate: { ideal: 24000 },
                    channelCount: 1,
                    echoCancellation: true,
                    noiseSuppression: true
                }
            });
            this.mediaStream = stream;
            this.initAudioContext();
            await this.audioContext.audioWorklet.addModule('/static/pcmWorkletProcessor.js');
            this.micWorkletNode = new AudioWorkletNode(this.audioContext, 'pcm-worklet-processor');

            this.micWorkletNode.port.onmessage = ({ data }) => {
                const incoming = new Int16Array(data);
                let read = 0;
                while (read < incoming.length) {
                    this.initBatch();
                    const toCopy = Math.min(
                        incoming.length - read,
                        this.BATCH_SAMPLES - this.batchOffset
                    );
                    this.batchInt16.set(
                        incoming.subarray(read, read + toCopy),
                        this.batchOffset
                    );
                    this.batchOffset += toCopy;
                    read += toCopy;
                    if (this.batchOffset === this.BATCH_SAMPLES) {
                        this.flushBatch();
                    }
                }
            };

            const source = this.audioContext.createMediaStreamSource(this.mediaStream);
            source.connect(this.micWorkletNode);
            this.statusDiv.textContent = "Recording...";
        } catch (err) {
            this.statusDiv.textContent = "Mic access denied.";
            console.error(err);
        }
    }

    async setupTTSPlayback() {
        await this.audioContext.audioWorklet.addModule('/static/ttsPlaybackProcessor.js');
        this.ttsWorkletNode = new AudioWorkletNode(
            this.audioContext,
            'tts-playback-processor'
        );

        this.ttsWorkletNode.port.onmessage = (event) => {
            const { type } = event.data;
            if (type === 'ttsPlaybackStarted') {
                if (!this.isTTSPlaying && this.socket && this.socket.readyState === WebSocket.OPEN) {
                    this.isTTSPlaying = true;
                    console.log(
                        "TTS playback started. Reason: ttsWorkletNode Event ttsPlaybackStarted."
                    );
                    this.socket.send(JSON.stringify({ type: 'tts_start' }));
                }
            } else if (type === 'ttsPlaybackStopped') {
                if (this.isTTSPlaying && this.socket && this.socket.readyState === WebSocket.OPEN) {
                    this.isTTSPlaying = false;
                    console.log(
                        "TTS playback stopped. Reason: ttsWorkletNode Event ttsPlaybackStopped."
                    );
                    this.socket.send(JSON.stringify({ type: 'tts_stop' }));
                }
            }
        };
        this.ttsWorkletNode.connect(this.audioContext.destination);
    }

    cleanupAudio() {
        if (this.micWorkletNode) {
            this.micWorkletNode.disconnect();
            this.micWorkletNode = null;
        }
        if (this.ttsWorkletNode) {
            this.ttsWorkletNode.disconnect();
            this.ttsWorkletNode = null;
        }
        if (this.audioContext) {
            this.audioContext.close();
            this.audioContext = null;
        }
        if (this.mediaStream) {
            this.mediaStream.getAudioTracks().forEach(track => track.stop());
            this.mediaStream = null;
        }
    }

    renderMessages() {
        this.messagesDiv.innerHTML = "";
        this.chatHistory.forEach(msg => {
            const bubble = document.createElement("div");
            bubble.className = `bubble ${msg.role}`;
            bubble.textContent = msg.content;
            this.messagesDiv.appendChild(bubble);
        });
        if (this.typingUser) {
            const typing = document.createElement("div");
            typing.className = "bubble user typing";
            typing.innerHTML = this.typingUser + '<span style="opacity:.6;">✏️</span>';
            this.messagesDiv.appendChild(typing);
        }
        if (this.typingAssistant) {
            const typing = document.createElement("div");
            typing.className = "bubble assistant typing";
            typing.innerHTML = this.typingAssistant + '<span style="opacity:.6;">✏️</span>';
            this.messagesDiv.appendChild(typing);
        }
        this.messagesDiv.scrollTop = this.messagesDiv.scrollHeight;
    }

    handleJSONMessage({ type, content }) {
        if (type === "partial_user_request") {
            this.typingUser = content?.trim() ? this.escapeHtml(content) : "";
            this.renderMessages();
            return;
        }
        if (type === "final_user_request") {
            if (content?.trim()) {
                this.chatHistory.push({ role: "user", content, type: "final" });
            }
            this.typingUser = "";
            this.renderMessages();
            return;
        }
        if (type === "partial_assistant_answer") {
            this.typingAssistant = content?.trim() ? this.escapeHtml(content) : "";
            this.renderMessages();
            return;
        }
        if (type === "final_assistant_answer") {
            if (content?.trim()) {
                this.chatHistory.push({ role: "assistant", content, type: "final" });
            }
            this.typingAssistant = "";
            this.renderMessages();
            return;
        }
        if (type === "tts_chunk") {
            if (this.ignoreIncomingTTS) return;
            const int16Data = this.base64ToInt16Array(content);
            if (this.ttsWorkletNode) {
                this.ttsWorkletNode.port.postMessage(int16Data);
            }
            return;
        }
        if (type === "tts_interruption") {
            if (this.ttsWorkletNode) {
                this.ttsWorkletNode.port.postMessage({ type: "clear" });
            }
            this.isTTSPlaying = false;
            this.ignoreIncomingTTS = false;
            return;
        }
        if (type === "user_speech_started") { // New message type from server
            console.log("Received user_speech_started from server.");
            this.canSendAudio = true;
            this._sendBufferedAudio(); // Send any buffered audio
            return;
        }
        if (type === "stop_tts") {
            if (this.ttsWorkletNode) {
                this.ttsWorkletNode.port.postMessage({ type: "clear" });
            }
            this.isTTSPlaying = false;
            this.ignoreIncomingTTS = true;
            console.log("TTS playback stopped. Reason: tts_interruption.");
            this.socket.send(JSON.stringify({ type: 'tts_stop' }));
            return;
        }
    }

    escapeHtml(str) {
        return (str ?? '')
            .replace(/&/g, "&amp;")
            .replace(/</g, "<")
            .replace(/>/g, ">")
            .replace(/"/g, "&quot;");
    }

    async start() {
        if (this.socket && this.socket.readyState === WebSocket.OPEN) {
            this.statusDiv.textContent = "Already recording.";
            return;
        }
        this.statusDiv.textContent = "Initializing connection...";

        const wsProto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        this.socket = new WebSocket(`${wsProto}//${location.host}/ws`);

        this.socket.onopen = async () => {
            this.statusDiv.textContent = "Connected. Activating mic and TTS…";
            // Audio capture starts, but sending is gated by `this.canSendAudio`
            await this.startRawPcmCapture();
            await this.setupTTSPlayback();
            this.speedSlider.disabled = false;
            // Inform the user they can start speaking, server will detect.
            // console.log("You can start speaking. Audio will be sent once speech is detected by the server.");
            // No, this message should come from the server or be part of a status update.
            // The client is now WAITING for the "user_speech_started" signal.
        };

        this.socket.onmessage = (evt) => {
            if (typeof evt.data === "string") {
                try {
                    const msg = JSON.parse(evt.data);
                    this.handleJSONMessage(msg);
                } catch (e) {
                    console.error("Error parsing message:", e);
                }
            }
        };

        this.socket.onclose = () => {
            this.statusDiv.textContent = "Connection closed.";
            this.flushRemainder();
            this.cleanupAudio();
            this.speedSlider.disabled = true;
        };

        this.socket.onerror = (err) => {
            this.statusDiv.textContent = "Connection error.";
            this.cleanupAudio();
            console.error(err);
            this.speedSlider.disabled = true;
        };
    }

    stop() {
        if (this.socket && this.socket.readyState === WebSocket.OPEN) {
            this.flushRemainder(); // This might try to send buffered audio if canSendAudio became true.
                                  // If stopping, we probably want to clear the buffer.
            this.socket.close();
        }
        this.cleanupAudio();
        this.statusDiv.textContent = "Stopped.";
        this.canSendAudio = false; // Reset flag
        this.audioBufferQueue = []; // Clear buffer on stop
    }

    clearHistory() {
        this.chatHistory = [];
        this.typingUser = "";
        this.typingAssistant = "";
        this.renderMessages();
        if (this.socket && this.socket.readyState === WebSocket.OPEN) {
            this.socket.send(JSON.stringify({ type: 'clear_history' }));
        }
    }

    setSpeed(speedValue) {
        if (this.socket && this.socket.readyState === WebSocket.OPEN) {
            this.socket.send(JSON.stringify({
                type: 'set_speed',
                speed: speedValue
            }));
        }
        console.log("Speed setting changed to:", speedValue);
    }

    copyConversation() {
        const text = this.chatHistory
            .map(msg => `${msg.role.charAt(0).toUpperCase() + msg.role.slice(1)}: ${msg.content}`)
            .join('\n');

        navigator.clipboard.writeText(text)
            .then(() => console.log("Conversation copied to clipboard"))
            .catch(err => console.error("Copy failed:", err));
    }
}
