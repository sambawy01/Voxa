// Plays audio as it arrives (ZONOS3 streaming): each chunk is scheduled right after the last.
export class LivePlayer {
  constructor() {
    this.ctx = new (window.AudioContext || window.webkitAudioContext)();
    this.ctx.resume().catch(() => {});
    this.at = 0;
  }
  /** Queue a Float32Array of samples recorded at `rate`. */
  push(samples, rate) {
    if (!samples.length) return;
    const buf = this.ctx.createBuffer(1, samples.length, rate);
    buf.copyToChannel(samples, 0);
    const src = this.ctx.createBufferSource();
    src.buffer = buf; src.connect(this.ctx.destination);
    this.at = Math.max(this.at, this.ctx.currentTime + 0.08);
    src.start(this.at);
    this.at += buf.duration;
  }
  /** Let what is queued finish, then release the audio device. */
  close() {
    const wait = Math.max(0, this.at - this.ctx.currentTime) + 0.5;
    setTimeout(() => this.ctx.close().catch(() => {}), wait * 1000);
  }
}
