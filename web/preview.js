// "Hear this voice" buttons, shared by every page with a voice picker. One clip plays at a
// time across the page. Studio voices play the server's preview line (GET /api/preview/<id>,
// made once per voice and free); on-device voices speak the same kind of line in the browser.

// The same lines the server uses for Studio previews (PREVIEW_LINES in server.py), and the
// same kind of line for the languages only on-device voices speak. Names are left out where
// the script is not Latin, so the pronunciation stays clean.
const LINES = {
  a: n => `Hi, I'm ${n}. This is how your words will sound in my voice.`,
  b: n => `Hello, I'm ${n}. This is how your words will sound in my voice.`,
  e: n => `Hola, soy ${n}. Así sonarán tus palabras con mi voz.`,
  f: n => `Bonjour, je suis ${n}. Voici comment vos mots sonneront avec ma voix.`,
  h: () => 'नमस्ते! आपके शब्द मेरी आवाज़ में कुछ ऐसे सुनाई देंगे।',
  i: n => `Ciao, sono ${n}. Ecco come suoneranno le tue parole con la mia voce.`,
  j: () => 'こんにちは。あなたの言葉は、私の声でこんなふうに聞こえます。',
  p: n => `Olá, eu sou ${n}. É assim que as suas palavras vão soar na minha voz.`,
  z: () => '你好！你的文字用我的声音读出来，就是这样的。',
  ar: () => 'مرحبًا! هكذا ستبدو كلماتك بصوتي.',
  bg: () => 'Здравейте! Ето как ще звучат думите ви с моя глас.',
  cs: n => `Ahoj, jsem ${n}. Takhle budou tvá slova znít mým hlasem.`,
  da: n => `Hej, jeg hedder ${n}. Sådan kommer dine ord til at lyde med min stemme.`,
  de: n => `Hallo, ich bin ${n}. So klingen deine Worte mit meiner Stimme.`,
  el: () => 'Γεια σας! Έτσι θα ακούγονται τα λόγια σας με τη φωνή μου.',
  et: n => `Tere, mina olen ${n}. Nii kõlavad sinu sõnad minu häälega.`,
  fi: n => `Hei, olen ${n}. Tältä sanasi kuulostavat minun äänelläni.`,
  hr: n => `Bok, ja sam ${n}. Ovako će tvoje riječi zvučati mojim glasom.`,
  hu: n => `Szia, ${n} vagyok. Így fognak szólni a szavaid az én hangomon.`,
  id: n => `Halo, saya ${n}. Beginilah kata-katamu akan terdengar dengan suaraku.`,
  ko: () => '안녕하세요! 당신의 글이 제 목소리로 이렇게 들립니다.',
  lt: n => `Labas, aš esu ${n}. Taip tavo žodžiai skambės mano balsu.`,
  lv: n => `Sveiki, es esmu ${n}. Tā tavi vārdi skanēs manā balsī.`,
  nl: n => `Hallo, ik ben ${n}. Zo klinken jouw woorden met mijn stem.`,
  pl: n => `Cześć, jestem ${n}. Tak będą brzmiały twoje słowa moim głosem.`,
  ro: n => `Bună, sunt ${n}. Așa vor suna cuvintele tale cu vocea mea.`,
  ru: () => 'Здравствуйте! Вот так ваши слова прозвучат моим голосом.',
  sk: n => `Ahoj, som ${n}. Takto budú tvoje slová znieť mojím hlasom.`,
  sl: n => `Živjo, sem ${n}. Tako bodo tvoje besede zvenele z mojim glasom.`,
  sv: n => `Hej, jag heter ${n}. Så här kommer dina ord att låta med min röst.`,
  tr: n => `Merhaba, ben ${n}. Sözlerin benim sesimle böyle duyulacak.`,
  uk: () => 'Вітаю! Ось так ваші слова звучатимуть моїм голосом.',
  vi: n => `Xin chào, tôi là ${n}. Đây là cách lời của bạn sẽ vang lên bằng giọng của tôi.`,
};
export const PREVIEW_LINE = (name, lang) => (LINES[lang] || LINES.a)(name);
// A tiny silent WAV, played inside the tap: iPhones only let a page play sound that a tap
// started, and a clip that arrives after an await would otherwise stay silent.
const SILENCE = 'data:audio/wav;base64,UklGRiYAAABXQVZFZm10IBAAAAABAAEAwF0AAIC7AAACABAAZGF0YQIAAAAAAA==';
const IDLE = '▶ Hear this voice', ON_DEVICE = new Map();
let player = null, current = null;

export function stopPreview() {
  if (player) { player.pause(); player.onended = player.onerror = null; }
  if (current) { current.button.textContent = IDLE; current.note.textContent = ''; }
  current = null;
}

/**
 * Wire a button to preview the voice its picker has selected.
 * voice(): {id, name, lite, key?} for the selected voice, or null.
 * speakOnDevice(voice, note): resolves to a Blob of WAV audio (on-device voices only).
 * key, when given, tells on-device clips apart (e.g. by expressiveness).
 */
export function attachPreview(button, note, voice, speakOnDevice) {
  button.textContent = IDLE;
  const me = { button, note };
  button.onclick = async () => {
    if (current === me) return stopPreview();
    stopPreview();
    const v = voice();
    if (!v || !v.id) return;
    current = me;
    button.textContent = '■ Stop';
    player ||= new Audio();
    player.src = SILENCE;
    player.play().catch(() => {});
    try {
      let src;
      if (v.lite) {
        const key = v.id + ':' + (v.key ?? '');
        if (!ON_DEVICE.has(key)) {
          note.textContent = 'Preparing the on-device voice…';
          const blob = await speakOnDevice(v, note);
          ON_DEVICE.set(key, URL.createObjectURL(blob));
        }
        src = ON_DEVICE.get(key);
      } else {
        note.textContent = 'Loading…';
        src = '/api/preview/' + encodeURIComponent(v.id);
      }
      if (current !== me) return;
      player.src = src;
      player.onended = () => { if (current === me) stopPreview(); };
      player.onerror = () => { if (current === me) { stopPreview(); note.textContent = 'The preview could not play. Try again in a moment.'; } };
      await player.play();
      if (current === me) note.textContent = `${v.name} · ${v.lite ? 'on this device' : 'Studio voice'}`;
    } catch (e) {
      if (current !== me) return;
      stopPreview();
      note.textContent = 'The preview could not play: ' + e.message;
    }
  };
  return me;
}
