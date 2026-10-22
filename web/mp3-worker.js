// Encodes audiobook chapters to MP3 off the main thread, so the page stays responsive.
import { encodeMp3 } from '/book.js?v={{BOOK_VERSION}}';

// lamejs is a classic script defining a global; evaluate Voxa's own copy of it here.
const lame = fetch('/static/lamejs-{{LAMEJS_VERSION}}/lame.min.js')
  .then(r => { if (!r.ok) throw new Error('MP3 encoder missing'); return r.text(); })
  .then(src => new Function(src + '\nreturn lamejs;')());

onmessage = async e => {
  const { id, pcm } = e.data;
  try {
    const mp3 = encodeMp3(await lame, pcm, undefined, undefined, progress => postMessage({ id, progress }));
    postMessage({ id, mp3 }, [mp3.buffer]);
  } catch (err) {
    postMessage({ id, error: err.message });
  }
};
