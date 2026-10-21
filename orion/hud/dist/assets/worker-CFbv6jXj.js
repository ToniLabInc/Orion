const e=/\/\*[\s\S]*?\*\//g,t=/--color-([a-z]+(?:-\d+)?):\s*oklch\(\s*([\d.]+)\s+([\d.]+)\s+([\d.]+)\s*\)/g,n=/--color-([a-z]+(?:-\d+)?):\s*var\(--color-([a-z]+(?:-\d+)?)\)/g,r=/\[data-state="(\w+)"\]\s*\{\s*--state:\s*var\(--color-([a-z]+(?:-\d+)?)\)/g,i=/:root\s*\{\s*--state:\s*var\(--color-([a-z]+(?:-\d+)?)\)/;function a(e,t,n){let r=n*Math.PI/180;return[e,t*Math.cos(r),t*Math.sin(r)]}function o(e){let t=e<=.0031308?12.92*e:1.055*e**(1/2.4)-.055;return Math.min(1,Math.max(0,t))}function s([e,t,n]){let r=(e+.3963377774*t+.2158037573*n)**3,i=(e-.1055613458*t-.0638541728*n)**3,a=(e-.0894841775*t-1.291485548*n)**3;return[o(4.0767416621*r-3.3077115913*i+.2309699292*a),o(-1.2684380046*r+2.6097574011*i-.3413193965*a),o(-.0041960863*r-.7034186147*i+1.707614701*a)]}function c(o){let s=o.replace(e,``),c=/* @__PURE__ */ new Map;for(let[,e,n,r,i]of s.matchAll(t))c.set(e,a(Number(n),Number(r),Number(i)));for(let[,e,t]of s.matchAll(n)){let n=c.get(t);n&&c.set(e,n)}let l=/* @__PURE__ */ new Map,u=i.exec(s);u&&l.set(`idle`,u[1]);for(let[,e,t]of s.matchAll(r))l.set(e,t);return{labs:c,tints:l}}let l;const u=/* @__PURE__ */ new Map;function d(){return l??=c(`@theme{--color-*:initial;--color-black:oklch(0.06 0.015 268);--color-white:oklch(0.985 0.004 255);--color-void-950:oklch(0.085 0.02 268);--color-void-900:oklch(0.11 0.024 268);--color-void-850:oklch(0.13 0.027 266);--color-void-800:oklch(0.155 0.03 264);--color-void-700:oklch(0.19 0.034 262);--color-void-600:oklch(0.235 0.036 260);--color-steel-50:oklch(0.975 0.006 255);--color-steel-100:oklch(0.935 0.009 255);--color-steel-200:oklch(0.87 0.013 255);--color-steel-300:oklch(0.785 0.017 255);--color-steel-400:oklch(0.675 0.02 255);--color-steel-500:oklch(0.605 0.02 255);--color-steel-600:oklch(0.46 0.019 255);--color-steel-700:oklch(0.37 0.017 255);--color-steel-800:oklch(0.28 0.014 255);--color-steel-900:oklch(0.21 0.012 255);--color-rigel-50:oklch(0.975 0.013 232);--color-rigel-100:oklch(0.945 0.031 232);--color-rigel-200:oklch(0.89 0.055 232);--color-rigel-300:oklch(0.845 0.086 232);--color-rigel-400:oklch(0.8 0.11 232);--color-rigel-500:oklch(0.72 0.108 232);--color-rigel-600:oklch(0.64 0.099 232);--color-rigel-700:oklch(0.55 0.086 232);--color-rigel-800:oklch(0.46 0.068 232);--color-rigel-900:oklch(0.37 0.053 232);--color-nebula-50:oklch(0.975 0.024 315);--color-nebula-100:oklch(0.945 0.056 315);--color-nebula-200:oklch(0.85 0.1 315);--color-nebula-300:oklch(0.775 0.156 315);--color-nebula-400:oklch(0.7 0.2 315);--color-nebula-500:oklch(0.62 0.196 315);--color-nebula-600:oklch(0.54 0.18 315);--color-nebula-700:oklch(0.45 0.156 315);--color-nebula-800:oklch(0.36 0.124 315);--color-nebula-900:oklch(0.27 0.096 315);--color-aurora-50:oklch(0.975 0.016 185);--color-aurora-100:oklch(0.945 0.036 185);--color-aurora-200:oklch(0.902 0.065 185);--color-aurora-300:oklch(0.866 0.101 185);--color-aurora-400:oklch(0.83 0.13 185);--color-aurora-500:oklch(0.75 0.127 185);--color-aurora-600:oklch(0.67 0.117 185);--color-aurora-700:oklch(0.58 0.101 185);--color-aurora-800:oklch(0.49 0.081 185);--color-aurora-900:oklch(0.4 0.062 185);--color-betel-50:oklch(0.975 0.02 50);--color-betel-100:oklch(0.945 0.048 50);--color-betel-200:oklch(0.87 0.085 50);--color-betel-300:oklch(0.81 0.133 50);--color-betel-400:oklch(0.75 0.17 50);--color-betel-500:oklch(0.67 0.167 50);--color-betel-600:oklch(0.59 0.153 50);--color-betel-700:oklch(0.5 0.133 50);--color-betel-800:oklch(0.41 0.105 50);--color-betel-900:oklch(0.32 0.082 50);--color-dusk-50:oklch(0.975 0.016 85);--color-dusk-100:oklch(0.945 0.036 85);--color-dusk-200:oklch(0.898 0.065 85);--color-dusk-300:oklch(0.859 0.101 85);--color-dusk-400:oklch(0.82 0.13 85);--color-dusk-500:oklch(0.74 0.127 85);--color-dusk-600:oklch(0.66 0.117 85);--color-dusk-700:oklch(0.57 0.101 85);--color-dusk-800:oklch(0.48 0.081 85);--color-dusk-900:oklch(0.39 0.062 85);--color-alert-50:oklch(0.975 0.025 25);--color-alert-100:oklch(0.945 0.059 25);--color-alert-200:oklch(0.842 0.105 25);--color-alert-300:oklch(0.761 0.164 25);--color-alert-400:oklch(0.68 0.21 25);--color-alert-500:oklch(0.6 0.206 25);--color-alert-600:oklch(0.52 0.189 25);--color-alert-700:oklch(0.43 0.164 25);--color-alert-800:oklch(0.34 0.13 25);--color-alert-900:oklch(0.25 0.101 25);--color-ok-50:oklch(0.975 0.018 150);--color-ok-100:oklch(0.945 0.042 150);--color-ok-200:oklch(0.882 0.075 150);--color-ok-300:oklch(0.831 0.117 150);--color-ok-400:oklch(0.78 0.15 150);--color-ok-500:oklch(0.7 0.147 150);--color-ok-600:oklch(0.62 0.135 150);--color-ok-700:oklch(0.53 0.117 150);--color-ok-800:oklch(0.44 0.093 150);--color-ok-900:oklch(0.35 0.072 150);--color-ok:var(--color-ok-400);}:root{--state:var(--color-rigel-400);}:root[data-state="listening"]{--state:var(--color-betel-400);}:root[data-state="thinking"]{--state:var(--color-nebula-400);}:root[data-state="speaking"]{--state:var(--color-aurora-400);}:root[data-state="loading"]{--state:var(--color-dusk-400);}:root[data-state="offline"]{--state:var(--color-steel-600);}`),l}function f(e){let t=d().labs.get(e);if(!t)throw Error(`palette.css has no --color-${e}`);return t}function p(e){let t=u.get(e);return t||(t=s(f(e)),u.set(e,t)),t}const m=e=>e<=.04045?e/12.92:((e+.055)/1.055)**2.4;function h(e){let[t,n,r]=p(e);return[m(t),m(n),m(r)]}function g([e,t,n]){let r=Math.cbrt(.4122214708*e+.5363325363*t+.0514459929*n),i=Math.cbrt(.2119034982*e+.6806995451*t+.1073969566*n),a=Math.cbrt(.0883024619*e+.2817188376*t+.6299787005*n);return[.2104542553*r+.793617785*i-.0040720468*a,1.9779984951*r-2.428592205*i+.4505937099*a,.0259040371*r+.7827717662*i-.808675766*a]}function _([e,t,n]){let r=(e+.3963377774*t+.2158037573*n)**3,i=(e-.1055613458*t-.0638541728*n)**3,a=(e-.0894841775*t-1.291485548*n)**3;return[Math.max(0,4.0767416621*r-3.3077115913*i+.2309699292*a),Math.max(0,-1.2684380046*r+2.6097574011*i-.3413193965*a),Math.max(0,-.0041960863*r-.7034186147*i+1.707614701*a)]}function v(e,t,n){let[,r,i]=g(e);return _([t,r*n,i*n])}function y(e,t,n){let r=g(e),i=g(t);return _([r[0]+(i[0]-r[0])*n,r[1]+(i[1]-r[1])*n,r[2]+(i[2]-r[2])*n])}const b=e=>h(`${e}-400`),x={idle:`rigel`,listening:`betel`,thinking:`nebula`,speaking:`aurora`,loading:`dusk`,error:`alert`},S={idle:`nebula`,listening:`alert`,thinking:`rigel`,speaking:`rigel`,loading:`betel`,error:`betel`};function ee(){let e={},t={};for(let[n,r]of Object.entries(x))e[n]=b(r),t[n]=v(y(b(r),b(S[n]),.45),.7,1.1);let n={};for(let e of[`rigel`,`nebula`,`aurora`,`betel`,`alert`,`ok`])n[e]=v(b(e),.86,1.05);let r=b(`nebula`);return{glow:e,upper:t,hues:n,sky:h(`void-950`),ground:h(`black`),dust:v(b(`rigel`),.82,.35),nebula:v(y(r,b(`betel`),.35),.72,.9),figure:v(b(`rigel`),.9,.45),auroraLow:v(r,.8,1.05),auroraHigh:v(y(b(`rigel`),b(`aurora`),.5),.62,1.1)}}const C=[{dpr:1,aurora:.5,sheets:1,faintest:5.5},{dpr:1.5,aurora:1,sheets:2,faintest:6.5},{dpr:2,aurora:1,sheets:2,faintest:6.5},{dpr:3,aurora:1.5,sheets:2,faintest:6.5}],w=C.length-1;function te(e){return e===`high`?w:e===`low`||e===`off`?1:2}var ne=class{tier;min;max;avg=1e3/60;slow=0;easy=0;hold=10;sinceUp=1/0;constructor(e,t){this.tier=this.min=this.max=0,this.prefer(e,t)}prefer(e,t){t===void 0?e===`auto`?(this.min=0,this.max=w,this.tier=te(e)):this.tier=this.min=this.max=te(e):this.tier=this.min=this.max=Math.min(w,Math.max(0,Math.round(t))),this.hold=10,this.reset()}reset(){this.avg=1e3/60,this.slow=0,this.easy=0}frame(e){if(this.min===this.max||!(e>0))return 0;let t=Math.min(e,250)/1e3;return this.avg+=(Math.min(e,250)-this.avg)*.1,this.sinceUp+=t,this.avg>18?(this.slow+=t,this.easy=0):(this.slow=0,this.easy=this.avg<17.5?this.easy+t:0),this.slow>=2&&this.tier>this.min?(this.sinceUp<6&&(this.hold=Math.min(160,this.hold*2)),this.tier--,this.sinceUp=1/0,this.reset(),-1):this.easy>=this.hold&&this.tier<this.max?(this.tier++,this.sinceUp=0,this.reset(),1):0}};function re(e,t){return t===null?e:Math.max(e,t*4)}var ie=class{debt=0;vsync=1e3/60;reset(){this.debt=0}due(e,t){this.vsync=e<this.vsync?e:this.vsync+(e-this.vsync)*.02;let n=1e3/t;return this.debt+=e,this.debt+this.vsync*.5<n?!1:(this.debt=Math.min(this.debt-n,n),!0)}};function ae(e,t,n){let r=e.createShader(t);if(!r)throw Error(`no shader`);if(e.shaderSource(r,n),e.compileShader(r),!e.getShaderParameter(r,e.COMPILE_STATUS)&&!e.isContextLost()){let t=e.getShaderInfoLog(r);throw e.deleteShader(r),Error(`shader: ${t}`)}return r}function T(e,t,n){let r=e.createProgram();if(!r)throw Error(`no program`);let i=ae(e,e.VERTEX_SHADER,t),a=ae(e,e.FRAGMENT_SHADER,n);if(e.attachShader(r,i),e.attachShader(r,a),e.linkProgram(r),e.deleteShader(i),e.deleteShader(a),!e.getProgramParameter(r,e.LINK_STATUS)&&!e.isContextLost())throw Error(`program: ${e.getProgramInfoLog(r)}`);let o={},s=e.getProgramParameter(r,e.ACTIVE_UNIFORMS);for(let t=0;t<s;t++){let n=e.getActiveUniform(r,t);n&&(o[n.name.replace(/\[0\]$/,``)]=e.getUniformLocation(r,n.name))}return{program:r,u:o}}function E(e,t,n,r,i,a=null,o=e.CLAMP_TO_EDGE){let s=e.createTexture();if(!s)throw Error(`no texture`);return e.bindTexture(e.TEXTURE_2D,s),e.texImage2D(e.TEXTURE_2D,0,r.internal,t,n,0,r.format,r.type,a),e.texParameteri(e.TEXTURE_2D,e.TEXTURE_MIN_FILTER,i),e.texParameteri(e.TEXTURE_2D,e.TEXTURE_MAG_FILTER,i===e.NEAREST?e.NEAREST:e.LINEAR),e.texParameteri(e.TEXTURE_2D,e.TEXTURE_WRAP_S,o),e.texParameteri(e.TEXTURE_2D,e.TEXTURE_WRAP_T,o),i===e.LINEAR_MIPMAP_LINEAR&&e.generateMipmap(e.TEXTURE_2D),s}function D(e,t,n,r,i,a=1){let o=e.createFramebuffer();if(!o)throw Error(`no framebuffer`);e.bindFramebuffer(e.FRAMEBUFFER,o);let s=[],c=[];for(let o=0;o<a;o++){let a=E(e,t,n,r,i);e.framebufferTexture2D(e.FRAMEBUFFER,e.COLOR_ATTACHMENT0+o,e.TEXTURE_2D,a,0),s.push(a),c.push(e.COLOR_ATTACHMENT0+o)}a>1&&e.drawBuffers(c);let l=e.checkFramebufferStatus(e.FRAMEBUFFER);if(e.bindFramebuffer(e.FRAMEBUFFER,null),l!==e.FRAMEBUFFER_COMPLETE&&!e.isContextLost()){e.deleteFramebuffer(o);for(let t of s)e.deleteTexture(t);throw Error(`framebuffer incomplete (${l.toString(16)})`)}return{fbo:o,tex:s,w:t,h:n}}function O(e){let t=e>>>0||1;return()=>{t=t+1831565813>>>0;let e=t;return e=Math.imul(e^e>>>15,e|1),e^=e+Math.imul(e^e>>>7,e|61),((e^e>>>14)>>>0)/4294967296}}const oe=[[[4,1]],[[16,1]],[[64,1]],[[32,.65],[64,.35]]],k=e=>e*e*e*(e*(e*6-15)+10);function A(e,t){let n=new Float32Array(e*e);for(let e=0;e<n.length;e++)n[e]=t();let r=/* @__PURE__ */ new Float32Array(65536),i=e/256;for(let t=0;t<256;t++){let a=t*i,o=Math.floor(a),s=k(a-o),c=o%e*e,l=(o+1)%e*e;for(let a=0;a<256;a++){let o=a*i,u=Math.floor(o),d=k(o-u),f=(u+1)%e,p=n[c+u%e]+(n[c+f]-n[c+u%e])*d,m=n[l+u%e]+(n[l+f]-n[l+u%e])*d;r[t*256+a]=p+(m-p)*s}}return r}function se(e=20){let t=O(e),n=/* @__PURE__ */ new Uint8Array(262144);return oe.forEach((e,r)=>{let i=/* @__PURE__ */ new Float32Array(65536);for(let[n,r]of e){let e=A(n,t);for(let t=0;t<i.length;t++)i[t]+=e[t]*r}let a=1/0,o=-1/0;for(let e of i)a=Math.min(a,e),o=Math.max(o,e);for(let e=0;e<i.length;e++)n[e*4+r]=Math.round((i[e]-a)/(o-a)*255)}),n}const j=4096,M=[1024,372,71,54];function N(e,t){let n=new Float32Array(e);for(let r=0;r<e;r++)n[r]=t();let r=new Float32Array(j),i=e/j;for(let t=0;t<j;t++){let a=t*i,o=Math.floor(a),s=k(a-o);r[t]=n[o%e]+(n[(o+1)%e]-n[o%e])*s}return r}function ce(e=41){let t=O(e),n=new Uint8Array(j*4);return M.forEach((e,r)=>{let i=N(e,t),a=1/0,o=-1/0;for(let e of i)a=Math.min(a,e),o=Math.max(o,e);for(let e=0;e<j;e++)n[e*4+r]=Math.round((i[e]-a)/(o-a)*255)}),n}const P={0:{advance:.64,strokes:[`A 0.32 0.5 0.235 0.5 95 460`]},1:{advance:.64,strokes:[`M 0.15 0.8 C 0.24 0.86 0.31 0.93 0.36 1 L 0.36 0`]},2:{advance:.64,strokes:[`M 0.1 0.74 C 0.1 0.92 0.22 1 0.32 1 C 0.44 1 0.54 0.92 0.54 0.76 C 0.54 0.62 0.46 0.52 0.36 0.42 L 0.08 0 L 0.56 0`]},3:{advance:.64,strokes:[`M 0.1 0.84 C 0.16 0.95 0.24 1 0.33 1 C 0.45 1 0.53 0.92 0.53 0.79 C 0.53 0.65 0.43 0.57 0.28 0.57 C 0.45 0.57 0.55 0.46 0.55 0.3 C 0.55 0.12 0.45 0 0.31 0 C 0.21 0 0.13 0.05 0.08 0.15`]},4:{advance:.64,strokes:[`M 0.56 0.3 L 0.06 0.3 L 0.41 1 L 0.41 0`]},5:{advance:.64,strokes:[`M 0.52 1 L 0.15 1 L 0.11 0.57 C 0.18 0.62 0.25 0.645 0.32 0.645 C 0.46 0.645 0.555 0.52 0.555 0.33 C 0.555 0.13 0.45 0 0.3 0 C 0.2 0 0.12 0.05 0.07 0.14`]},6:{advance:.64,strokes:[`M 0.5 0.93 C 0.45 0.98 0.39 1 0.33 1 C 0.17 1 0.08 0.8 0.08 0.48 C 0.08 0.17 0.18 0 0.32 0 C 0.46 0 0.555 0.12 0.555 0.3 C 0.555 0.49 0.45 0.6 0.32 0.6 C 0.2 0.6 0.11 0.51 0.085 0.37`]},7:{advance:.64,strokes:[`M 0.08 1 L 0.56 1 C 0.42 0.74 0.3 0.42 0.24 0`]},8:{advance:.64,strokes:[`M 0.32 0.56 C 0.18 0.6 0.115 0.68 0.115 0.785 C 0.115 0.92 0.205 1 0.32 1 C 0.435 1 0.525 0.92 0.525 0.785 C 0.525 0.68 0.46 0.6 0.32 0.56 C 0.165 0.515 0.085 0.42 0.085 0.28 C 0.085 0.11 0.19 0 0.32 0 C 0.45 0 0.555 0.11 0.555 0.28 C 0.555 0.42 0.475 0.515 0.32 0.56`]},9:{advance:.64,strokes:[`M 0.545 0.66 C 0.51 0.52 0.42 0.42 0.3 0.42 C 0.17 0.42 0.085 0.53 0.085 0.7 C 0.085 0.88 0.185 1 0.315 1 C 0.465 1 0.555 0.86 0.555 0.6 C 0.555 0.24 0.44 0 0.26 0 C 0.18 0 0.12 0.03 0.085 0.08`]},":":{advance:.3,strokes:[`A 0.15 0.68 0.03 0.03 90 450`,`A 0.15 0.16 0.03 0.03 90 450`]},".":{advance:.26,strokes:[`A 0.13 0.035 0.03 0.03 90 450`]},",":{advance:.26,strokes:[`M 0.14 0.07 C 0.15 0.02 0.13 -0.07 0.08 -0.14`]},"°":{advance:.4,strokes:[`A 0.2 0.86 0.11 0.11 90 455`]},"%":{advance:.66,strokes:[`A 0.16 0.82 0.1 0.13 90 455`,`M 0.53 1 L 0.11 0`,`A 0.48 0.18 0.1 0.13 90 455`]},"+":{advance:.64,strokes:[`M 0.32 0.22 L 0.32 0.74`,`M 0.08 0.48 L 0.56 0.48`]},"-":{advance:.48,strokes:[`M 0.08 0.46 L 0.4 0.46`]},"−":{advance:.64,strokes:[`M 0.08 0.46 L 0.56 0.46`]},"/":{advance:.54,strokes:[`M 0.46 1.04 L 0.08 -0.04`]}," ":{advance:.32,strokes:[]},h:{advance:.63,strokes:[`M 0.1 1 L 0.1 0 L 0.1 0.42 C 0.15 0.6 0.24 0.7 0.34 0.7 C 0.46 0.7 0.53 0.62 0.53 0.46 L 0.53 0`]},m:{advance:.82,strokes:[`M 0.09 0.7 L 0.09 0 L 0.09 0.44 C 0.12 0.61 0.19 0.7 0.27 0.7 C 0.36 0.7 0.41 0.63 0.41 0.5 L 0.41 0 L 0.41 0.5 C 0.44 0.63 0.5 0.7 0.59 0.7 C 0.68 0.7 0.73 0.63 0.73 0.5 L 0.73 0`]},s:{advance:.56,strokes:[`M 0.47 0.61 C 0.42 0.67 0.35 0.7 0.28 0.7 C 0.17 0.7 0.1 0.64 0.1 0.54 C 0.1 0.43 0.19 0.4 0.29 0.37 C 0.41 0.34 0.48 0.29 0.48 0.18 C 0.48 0.07 0.39 0 0.28 0 C 0.19 0 0.12 0.03 0.07 0.1`]},a:{advance:.6,strokes:[`M 0.5 0.56 C 0.46 0.65 0.39 0.7 0.3 0.7 C 0.17 0.7 0.08 0.57 0.08 0.35 C 0.08 0.13 0.17 0 0.3 0 C 0.39 0 0.46 0.05 0.5 0.14 L 0.5 0.7 L 0.5 0`]},p:{advance:.62,strokes:[`M 0.1 0.7 L 0.1 -0.3 L 0.1 0.36 C 0.1 0.58 0.2 0.7 0.32 0.7 C 0.46 0.7 0.54 0.56 0.54 0.35 C 0.54 0.14 0.45 0 0.32 0 C 0.2 0 0.1 0.12 0.1 0.32`]},C:{advance:.65,strokes:[`M 0.57 0.85 C 0.51 0.95 0.42 1 0.33 1 C 0.18 1 0.08 0.8 0.08 0.5 C 0.08 0.2 0.18 0 0.33 0 C 0.42 0 0.51 0.05 0.57 0.15`]},F:{advance:.58,strokes:[`M 0.53 1 L 0.1 1 L 0.1 0`,`M 0.1 0.53 L 0.45 0.53`]},question:{advance:.62,strokes:[`M 0.1 0.79 C 0.12 0.94 0.22 1 0.32 1 C 0.45 1 0.54 0.91 0.54 0.78 C 0.54 0.64 0.45 0.58 0.37 0.52 C 0.31 0.48 0.3 0.42 0.3 0.3`,`A 0.3 0.04 0.035 0.035 90 450`]},check:{advance:.84,strokes:[`M 0.06 0.44 L 0.27 0.16 C 0.38 0.4 0.52 0.68 0.76 0.96`]}},le=.0025;function ue(e){let t=e.trim().split(/\s+/),n=[],r=0,i=()=>{let n=Number(t[r++]);if(!Number.isFinite(n))throw Error(`a number expected in "${e}"`);return n},a=null;for(;r<t.length;){let o=t[r++];if(o===`M`){if(a)throw Error(`a stroke moves only at its start: "${e}"`);a=[i(),i()],n.push(a)}else if(o===`L`){if(!a)throw Error(`a stroke starts with M or A: "${e}"`);a=[i(),i()],n.push(a)}else if(o===`C`){if(!a)throw Error(`a stroke starts with M or A: "${e}"`);let t=a,r=[i(),i()],o=[i(),i()],s=[i(),i()],c=Math.max(Math.hypot(t[0]-2*r[0]+o[0],t[1]-2*r[1]+o[1]),Math.hypot(r[0]-2*o[0]+s[0],r[1]-2*o[1]+s[1])),l=Math.max(2,Math.ceil(Math.sqrt(3*c/(4*le))));for(let e=1;e<=l;e++){let i=e/l,a=1-i;n.push([a*a*a*t[0]+3*a*a*i*r[0]+3*a*i*i*o[0]+i*i*i*s[0],a*a*a*t[1]+3*a*a*i*r[1]+3*a*i*i*o[1]+i*i*i*s[1]])}a=s}else if(o===`A`){let[e,t,r,o,s,c]=[i(),i(),i(),i(),i(),i()],l=(c-s)*Math.PI/180,u=2*Math.acos(1-le/Math.max(r,o)),d=Math.max(6,Math.ceil(Math.abs(l)/u)),f=s*Math.PI/180;for(let i=+!!a;i<=d;i++){let a=f+l*i/d;n.push([e+r*Math.cos(a),t+o*Math.sin(a)])}a=n[n.length-1]}else throw Error(`unknown command "${o}" in "${e}"`)}if(!n.length)throw Error(`an empty stroke: "${e}"`);return n}const F=12*4**(1/3),I=3.5,de=[.8,1.45];function L(e){let t=e.length,n=t=>Math.hypot(e[t+1][0]-e[t][0],e[t+1][1]-e[t][1]),r=[],i=0,a=e.slice(1).reduce((e,t,r)=>e+n(r),0);for(let o=0;o<t;o++){let s=0;if(o>0&&o<t-1){let t=Math.atan2(e[o][1]-e[o-1][1],e[o][0]-e[o-1][0]),r=Math.atan2(e[o+1][1]-e[o][1],e[o+1][0]-e[o][0]),i=Math.abs(r-t);i>Math.PI&&(i=2*Math.PI-i);let a=(n(o-1)+n(o))/2;s=a>0?i/a:0}let c=s>0?Math.min(26,F*s**(-1/3)):26,l=Math.min(1,i/.09,(a-i)/.07);c=I+(c-I)*Math.max(0,l),r.push(c),o<t-1&&(i+=n(o))}return r.map((e,t)=>Math.min(e,((r[t-1]??e)+e+(r[t+1]??e))/3))}const fe=e=>Math.min(de[1],Math.max(de[0],Math.sqrt(13/e)));function R(e,t,n=1){let r=[],i=0;for(let t of e){let e=P[t];if(!e)throw Error(`the pen has no glyph for "${t}"`);for(let t of e.strokes)r.push(ue(t).map(([e,t])=>[i+e,t]));i+=e.advance}let a=1/0,o=-1/0;for(let e of r)for(let[t]of e)a=Math.min(a,t),o=Math.max(o,t);if(!r.length)return{segments:[],duration:0,width:0,height:t};let s=(a+o)/2,c=[],l=0,u=null;for(let e of r){u&&(l+=(.05+Math.hypot(e[0][0]-u[0],e[0][1]-u[1])/30)/n);let r=L(e).map(e=>e*n);for(let i=0;i<e.length-1;i++){let[a,o]=e[i],[u,d]=e[i+1],f=Math.hypot(u-a,d-o)/((r[i]+r[i+1])/2);c.push({x0:(a-s)*t,y0:(.5-o)*t,x1:(u-s)*t,y1:(.5-d)*t,t0:l,t1:l+f,ink0:fe(r[i]/n),ink1:fe(r[i+1]/n)}),l+=f}u=e[e.length-1]}return{segments:c,duration:l,width:(o-a)*t,height:t}}const z=Math.PI/180;function pe(e){let t=new DataView(e);if(String.fromCharCode(t.getUint8(0),t.getUint8(1),t.getUint8(2),t.getUint8(3))!==`HZSK`||t.getUint8(4)!==1)throw Error(`stars.bin is not a version 1 star catalogue`);let n=t.getUint16(6,!0),r=t.getUint16(8,!0),i=12+n*2,a=i+n*2,o=a+n,s=o+n;if(e.byteLength!==s+r*4)throw Error(`stars.bin is cut short or too long`);let c=new Float32Array(n*3),l=new Float32Array(n),u=new Float32Array(n);for(let e=0;e<n;e++){let n=t.getUint16(12+e*2,!0)/65536*2*Math.PI,r=t.getInt16(i+e*2,!0)/32767*90*z;c[e*3]=Math.cos(r)*Math.cos(n),c[e*3+1]=Math.cos(r)*Math.sin(n),c[e*3+2]=Math.sin(r),l[e]=t.getUint8(a+e)/25-2,u[e]=t.getInt8(o+e)/50}let d=new Uint16Array(r*2);for(let e=0;e<r*2;e++)d[e]=t.getUint16(s+e*2,!0);return{count:n,dirs:c,vmag:l,bv:u,lines:d}}function B(e,t){let n=e*15*z,r=t*z;return[Math.cos(r)*Math.cos(n),Math.cos(r)*Math.sin(n),Math.sin(r)]}function V(e){let t=Math.min(2.5,Math.max(-.4,e));return 4600*(1/(.92*t+1.7)+1/(.92*t+.62))}function H(e){let t=Math.min(25e3,Math.max(1667,e)),n=1e9/t**3,r=1e6/t**2,i=1e3/t,a=t<=4e3?-.2661239*n-.2343589*r+.8776956*i+.17991:-3.0258469*n+2.1070379*r+.2226347*i+.24039;return[a,t<=2222?-1.1063814*a**3-1.3481102*a**2+2.18555832*a-.20219683:t<=4e3?-.9549476*a**3-1.37418593*a**2+2.09137015*a-.16748867:3.081758*a**3-5.8733867*a**2+3.75112997*a-.37001483]}const U=.72;function W(e){let[t,n]=H(V(e)),r=t/n,i=(1-t-n)/n,a=Math.max(0,3.2404542*r-1.5371385-.4985314*i),o=Math.max(0,-.969266*r+1.8760108+.041556*i),s=Math.max(0,.0556434*r-.2040259+1.0572252*i),c=.2126*a+.7152*o+.0722*s;a=1+(a/c-1)*U,o=1+(o/c-1)*U,s=1+(s/c-1)*U;let l=.2126*a+.7152*o+.0722*s;return[a/l,o/l,s/l]}const G={ra:5+35/60,dec:-1},me={ra:5+35/60+17.3/3600,dec:-5.391111111111112,sx:.32,sy:.42};function he(e,t){let n=e*15*z,r=t*z,i=[Math.cos(r)*Math.cos(n),Math.cos(r)*Math.sin(n),Math.sin(r)],a=[-Math.sin(n),Math.cos(n),0],o=[-Math.sin(r)*Math.cos(n),-Math.sin(r)*Math.sin(n),Math.cos(r)];return new Float32Array([-a[0],-a[1],-a[2],o[0],o[1],o[2],i[0],i[1],i[2]])}function ge(e,t,n,r){let i=e[0]*t+e[1]*n+e[2]*r,a=e[3]*t+e[4]*n+e[5]*r,o=e[6]*t+e[7]*n+e[8]*r;if(o<=-.999)return null;let s=2/(1+o);return[i*s,a*s]}const _e=[-.8676661,-.1980764,.4559838];function ve(e,t,n,r,i){let a=n*t-.5,o=r*t-.5,s=Math.floor(a),c=Math.floor(o),l=a-s,u=o-c,d=(n,r)=>e[((r%t+t)%t*t+(n%t+t)%t)*4+i],f=d(s,c)+(d(s+1,c)-d(s,c))*l;return(f+(d(s,c+1)+(d(s+1,c+1)-d(s,c+1))*l-f)*u)/255}function ye(e,t,n,r,i,a){let o=Math.ceil((e+64)/5),s=Math.ceil((t+64)/5),c=new Uint8Array(o*s),[l,u,d]=_e;for(let f=0;f<s;f++){let p=-32+(f+.5)*(t+64)/s;for(let t=0;t<o;t++){let s=(-32+(t+.5)*(e+64)/o-n.x)/n.scale,m=(n.y-p)/n.scale,h=s*s+m*m,g=1/(4+h),_=4*s*g,v=4*m*g,y=(4-h)*g,b=(_*r[0]+v*r[3]+y*r[6])*l+(_*r[1]+v*r[4]+y*r[7])*u+(_*r[2]+v*r[5]+y*r[8])*d,x=.35+.9*ve(i,a,s*1.9+.17,m*1.9+.17,1);c[f*o+t]=Math.round(255*Math.min(1,Math.exp(-Math.abs(b)/.16)*x/1.25))}}return{data:c,w:o,h:s}}function be(e){let t=he(G.ra,G.dec),n=-1/0,r=1/0;for(let i of e.lines){let a=ge(t,e.dirs[i*3],e.dirs[i*3+1],e.dirs[i*3+2]);a&&(n=Math.max(n,a[1]),r=Math.min(r,a[1]))}return{top:n,bottom:r}}function xe(e,t,n){let r=Math.max(1,t),i=.72*r/(n.top-n.bottom),a=e/(4*Math.tan(125*z/4)),o=Math.max(i,a);return{scale:o,x:e/2,y:.11*r+n.top*o}}const Se=1/800,K=Array.from({length:16},(e,t)=>t+2),Ce=6.5,q=.02,we=1/1.1,J=1/511,Y=(we*.00125/J)**2;var X=class{u=/* @__PURE__ */ new Float32Array(512);prev=/* @__PURE__ */ new Float32Array(512);next=/* @__PURE__ */ new Float32Array(512);lap=/* @__PURE__ */ new Float32Array(512);lapPrev=/* @__PURE__ */ new Float32Array(512);force=/* @__PURE__ */ new Float32Array(512);shown=/* @__PURE__ */ new Float32Array(512);shownPrev=/* @__PURE__ */ new Float32Array(512);shapes;gains;omegas;phases;bands=new Float32Array(K.length);targets=new Float32Array(K.length);time=0;owed=0;steps=0;mic=0;average=0;kick=0;onsetAt=-1/0;phaseAt=0;bursts=[];still=!0;talk=.5;constructor(){let e=K.length;this.shapes=new Float32Array(e*512),this.gains=new Float32Array(e),this.omegas=new Float32Array(e),this.phases=new Float32Array(e);for(let t=0;t<e;t++){let e=K[t]*Math.PI,n=we*e,r=1.6+3e-4*e*e,i=9*(K[0]/K[t])**.25;this.omegas[t]=n,this.gains[t]=2*r*n*i,this.phases[t]=t*2.399963%(2*Math.PI);for(let n=0;n<512;n++)this.shapes[t*512+n]=Math.sin(e*n*J)}}voice(e){for(let t=0;t<K.length;t++)this.targets[t]=t<e.length?Math.min(1,Math.max(0,e[t])):0;this.targets.some(e=>e>q)&&(this.still=!1)}listen(e){this.mic=Math.min(1,Math.max(0,e)),this.mic>q&&(this.still=!1)}pluck(e,t){this.bursts.push({at:Math.min(1,Math.max(0,e)),px:t,start:this.time}),this.still=!1}silence(){this.targets.fill(0),this.bands.fill(0),this.mic=this.average=this.kick=0,this.bursts=[],this.u.fill(0),this.prev.fill(0),this.lap.fill(0),this.lapPrev.fill(0),this.still=!0}advance(e){if(this.still){this.owed=0;return}for(this.owed+=Math.min(e,.1),this.follow(e);this.owed>=Se;)this.owed-=Se,this.steps++%4==0&&this.drive(),this.step(),this.time+=Se;this.settle()}follow(e){for(let t=0;t<K.length;t++){let n=this.targets[t],r=n>this.bands[t]?.05:.16;this.bands[t]+=(n-this.bands[t])*(1-Math.exp(-e/r))}this.average+=(this.mic-this.average)*(1-Math.exp(-e/.25)),this.kick*=Math.exp(-e/.14);let t=this.mic-this.average;t>.07&&this.time-this.onsetAt>.12&&(this.kick=Math.min(1,this.kick+t*4),this.onsetAt=this.phaseAt=this.time)}drive(){let e=this.force;e.fill(0);let t=this.time;for(let n=0;n<K.length;n++){let r=this.bands[n];if(r<1e-4)continue;let i=this.gains[n]*r*Math.sin(this.omegas[n]*t+this.phases[n]),a=this.shapes.subarray(n*512,(n+1)*512);for(let t=1;t<511;t++)e[t]+=i*a[t]}let n=2*Math.PI*Ce,r=1.1*this.average+4.2*this.kick;r>.001&&this.source(this.talk,2*we*n*r*Math.sin(n*(t-this.phaseAt))),this.bursts=this.bursts.filter(e=>{let r=t-e.start,i=1.5;if(r>i/6.5)return!1;let a=Math.sin(Math.PI*r*Ce/i)**2;return this.source(e.at,2*we*n*e.px*a*Math.sin(n*r)),!0})}source(e,t){let n=e*511,r=t/(.006*Math.sqrt(2*Math.PI)),i=Math.max(1,Math.floor(n-13)),a=Math.min(510,Math.ceil(n+13));for(let e=i;e<=a;e++){let t=(e-n)/3.0660000000000003;this.force[e]+=r*Math.exp(-.5*t*t)}}step(){let{u:e,prev:t,next:n,lap:r,lapPrev:i,force:a}=this;for(let t=1;t<511;t++)r[t]=e[t+1]-2*e[t]+e[t-1];for(let o=1;o<511;o++)n[o]=(2*e[o]-.998*t[o]+Y*r[o]+.19584075*(r[o]-i[o])+15625e-10*a[o])/1.002;this.prev=e,this.u=n,this.next=t,this.lapPrev=r,this.lap=i}settle(){if(this.mic>q||this.kick>.001||this.bursts.length||this.bands.some(e=>e>q))return;let e=0,t=0;for(let n=0;n<512;n++)e=Math.max(e,Math.abs(this.u[n])),t=Math.max(t,Math.abs(this.u[n]-this.prev[n]));e<.02&&t/.00125<.5&&this.silence()}peak(){let e=0;for(let t=0;t<512;t++)e=Math.max(e,Math.abs(this.u[t]));return e}write(e,t){let{u:n,prev:r,shown:i,shownPrev:a}=this,o=t/511;for(let e=0;e<512;e++)i[e]=26*Math.tanh(n[e]/26),a[e]=26*Math.tanh(r[e]/26);for(let t=0;t<512;t++){let n=t>0?i[t-1]:-i[1],r=t<511?i[t+1]:-i[510];e[t*4]=i[t],e[t*4+1]=(r-n)/(2*o),e[t*4+2]=Math.abs(i[t]-a[t])/Se*.016666666666666666*.5,e[t*4+3]=0}}};function Te(e,t){let n=3.2*Math.max(1,e),r=n*Math.asin(Math.min(1,(e/2+24)/n));return{cx:e/2,cy:t+n,r:n,horizon:t,length:2*r,start:-r}}function Ee(e,t,n){return e.r*Math.atan2(t-e.cx,e.cy-n)}function De(e,t){let n=t/e.r,r=Math.sin(n),i=-Math.cos(n);return{x:e.cx+e.r*r,y:e.cy+e.r*i,nx:r,ny:i}}function Oe(e,t){return Math.min(1,Math.max(0,(t-e.start)/e.length))}const ke=Math.PI/180,Ae={share:.6,min:140,max:520},je={min:.7,max:2.6},Me=.3,Ne=.04,Pe=.15,Fe=.5,Ie=.12,Le={share:.16,min:56,max:150},Z=.9,Re={x:18,y:7,px:540,py:810},ze={x:5,y:3,follow:.5},Be=[];var Ve=class{palette;reduced;rng=O(2026);width=1;height=1;horizon=1;talkX=0;talkY=0;laid=!1;limb=Te(1,1);talkArc=0;sky=null;starColors=/* @__PURE__ */ new Float32Array;skyOpen=0;view=he(G.ra,G.dec);frame={scale:1,x:0,y:0};span=null;m42=[0,0,1,1];figureTimes=/* @__PURE__ */ new Float32Array;figureRev=0;shift=[0,0];time=0;state=`idle`;from=`idle`;to=`idle`;wipeFront=1/0;wipeEnd=0;glow=1;errorAt=-1/0;strand=new X;stringData=/* @__PURE__ */ new Float32Array(2048);stringRev=0;stringMoved=!0;voice=0;mic=0;voiceTarget=0;micTarget=0;voiceAt=-1/0;micAt=-1/0;aurora=0;auroraDrift=0;auroraTop=0;sweep=-1e4;resting=!1;restMix=0;figureClock=0;figureAlpha=0;bumps=[];bumpData=/* @__PURE__ */ new Float32Array(16);meteors=[];recalls=[];writing=null;writingRev=0;pointerTarget=[0,0];parallax=[0,0];constructor(e,t){this.palette=e,this.reduced=t}size(e,t){this.width=Math.max(1,e),this.height=Math.max(1,t),this.rebuild()}layout(e,t,n){this.horizon=e,this.talkX=t,this.talkY=n,this.laid=!0,this.rebuild()}setSky(e){this.sky=e,this.starColors=new Float32Array(e.count*3);for(let t=0;t<e.count;t++)this.starColors.set(W(e.bv[t]),t*3);this.span=be(e),this.rebuild()}rebuild(){if(!this.laid)return;let e=Math.min(Math.max(this.horizon,1),this.height);if(this.limb=Te(this.width,e),this.talkArc=Ee(this.limb,this.talkX,this.talkY),this.strand.talk=Oe(this.limb,this.talkArc),this.auroraTop=Math.min(Ae.max,Math.max(Ae.min,e*Ae.share)),!this.sky||!this.span)return;this.frame=xe(this.width,e,this.span);let t=ge(this.view,...B(me.ra,me.dec));t&&(this.m42=[t[0],t[1],me.sx*ke,me.sy*ke]);let n=this.sky.lines,r=new Float32Array(n.length);for(let e=0;e<n.length/2;e++){let t=this.plane(n[e*2]),i=this.plane(n[e*2+1]),a=t&&i?Math.hypot(t[0]-i[0],t[1]-i[1])*this.frame.scale:0;r[e*2]=.8+e*.42,r[e*2+1]=Math.min(je.max,Math.max(je.min,a/110))}this.figureTimes=r,this.figureRev++}plane(e){let t=this.sky?.dirs;return t?ge(this.view,t[e*3],t[e*3+1],t[e*3+2]):null}starAt(e){let t=this.plane(e);return t?[this.frame.x+t[0]*this.frame.scale+this.shift[0],this.frame.y-t[1]*this.frame.scale+this.shift[1]]:null}setState(e){if(e===this.state)return;this.state=e,this.from=this.to,this.to=e;let t=Math.max(Math.abs(this.talkArc-this.limb.start),Math.abs(this.limb.start+this.limb.length-this.talkArc));this.wipeFront=0,this.wipeEnd=t+160,e===`listening`?(this.strand.pluck(this.strand.talk,2.4),this.bump(this.talkArc,90,.5,.5)):e===`error`?(this.errorAt=this.time,this.strand.pluck(this.strand.talk,6),this.bump(this.talkArc,140,1.2,.6)):(e===`thinking`||e===`speaking`)&&this.bump(this.talkArc,120,.6,.6)}level(e,t,n){e===1?(this.voiceTarget=t,this.voiceAt=this.time,this.strand.voice(n)):(this.micTarget=t,this.micAt=this.time,this.strand.listen(t))}pointer(e,t){this.pointerTarget=[e,t]}meteor(e,t,n){if(!this.laid)return;let r=this.talkArc+(this.rng()-.5)*.08*this.width,i=De(this.limb,r),a=Math.hypot(t-i.x,n-i.y);this.meteors.length>=8&&this.meteors.shift(),this.meteors.push({t0:this.time,ox:i.x,oy:i.y-1,tx:t,ty:n,dur:.5+a*35e-5,colour:this.palette.hues[e]}),this.bump(r,70,1,.45)}bump(e,t,n,r){let i=e=>e.amp*Math.exp(-(this.time-e.t0)/e.tau);if(this.bumps.length>=4){let e=0;for(let t=1;t<this.bumps.length;t++)i(this.bumps[t])<i(this.bumps[e])&&(e=t);this.bumps.splice(e,1)}this.bumps.push({s:e,sigma:t,amp:n,t0:this.time,tau:r})}recall(e){let t=this.sky;if(!this.laid||!t)return;let n=Math.min(8,Math.max(0,Math.floor(e))),r=[];for(let e=0;e<t.count&&t.vmag[e]<=4.6;e++){let t=this.starAt(e);t&&t[1]>72&&t[1]<this.horizon*.8&&t[0]>24&&t[0]<this.width-24&&r.push({i:e,x:t[0],y:t[1]})}let i=[];for(;i.length<n&&r.length;){let e=r.splice(Math.floor(this.rng()*r.length),1)[0];i.every(t=>Math.hypot(t.x-e.x,t.y-e.y)>=90)&&i.push(e)}i.forEach((e,t)=>{this.recalls.push({star:e.i,t0:this.time+t*.2})})}write(e,t,n){let r=e??t??``;if(n===0){this.writing&&(r===``||this.writing.key===r)&&(this.writing.fadeAt=Math.min(this.writing.fadeAt,this.time));return}if(!this.laid)return;let i=e===null?[t]:[...e],a=R(i,100,1).width,o=R(i,Math.min(Le.max,Math.max(Le.min,this.horizon*Le.share),a>0?(this.width-32)/a*100:Le.max),e===null?.45:1),s=o.width/2+16,c=t===`question`?this.palette.hues.betel:t===`check`?this.palette.hues.ok:Ue(this.palette.glow[this.state],.55);this.writing={key:r,segments:o.segments,duration:o.duration,t0:this.time,fadeAt:this.time+o.duration+n/1e3,origin:[Math.min(Math.max(this.talkX,s),Math.max(s,this.width-s)),this.horizon*.42],colour:c,pulse:t===`question`,rev:++this.writingRev}}rest(e){e!==this.resting&&(this.resting=e,e&&(this.figureClock=0))}update(e){this.time+=e;let t=this.time,n=t=>1-Math.exp(-e/t);t-this.voiceAt>Me&&this.voiceTarget>0&&(this.voiceTarget=0,this.strand.voice(Be)),t-this.micAt>Me&&this.micTarget>0&&(this.micTarget=0,this.strand.listen(0)),this.voice+=(this.voiceTarget-this.voice)*n(this.voiceTarget>this.voice?Ne:Pe),this.mic+=(this.micTarget-this.mic)*n(this.micTarget>this.mic?Ne:Pe);let r=!this.strand.still;(r||this.stringMoved)&&(this.strand.advance(e),this.strand.write(this.stringData,this.limb.length),this.stringRev++,this.stringMoved=r),this.wipeFront<this.wipeEnd&&(this.wipeFront+=2600*e,this.wipeFront>=this.wipeEnd&&(this.wipeFront=1/0,this.from=this.to)),this.restMix+=(+!!this.resting-this.restMix)*n(this.resting?2.5:.35);let i=t-this.errorAt,a=i<3?Math.exp(-i/.5)*(.5+.5*Math.sin(Math.PI*18*i)):0;this.glow=(1-.5800000000000001*this.restMix)*(1+.6*a),this.sweep=this.state===`loading`?this.limb.start+(t/2.2%1*1.2-.1)*this.limb.length:-1e4;let o=+(this.state===`thinking`);this.aurora+=(o-this.aurora)*n(o>this.aurora?.9:1.4),this.aurora>.004?this.auroraDrift+=e*25:(this.aurora=o===0?0:this.aurora,this.auroraDrift=0),this.resting?(this.figureClock+=e,this.figureAlpha+=(1-this.figureAlpha)*n(1.2)):(this.figureAlpha+=(0-this.figureAlpha)*n(.25),this.figureAlpha<.003&&(this.figureAlpha=0)),this.skyOpen=this.sky?Math.min(1,this.skyOpen+e/1.6):0,this.bumps=this.bumps.filter(e=>e.amp*Math.exp(-(t-e.t0)/e.tau)>.01),this.meteors=this.meteors.filter(e=>t-e.t0<e.dur+Fe+.05),this.recalls=this.recalls.filter(e=>t-e.t0<9.12),this.writing&&t>this.writing.fadeAt+Z&&(this.writing=null),this.lean(e),this.placeSky()}lean(e){let t=1-Math.exp(-e/ze.follow);this.parallax[0]+=(this.pointerTarget[0]-this.parallax[0])*t,this.parallax[1]+=(this.pointerTarget[1]-this.parallax[1])*t}placeSky(){let e=this.time;this.shift[0]=Re.x*Math.sin(2*Math.PI*e/Re.px)-this.parallax[0]*ze.x,this.shift[1]=Re.y*Math.sin(2*Math.PI*e/Re.py+1)-this.parallax[1]*ze.y}settle(){this.from=this.to=this.state,this.wipeFront=1/0,this.voice=this.mic=this.voiceTarget=this.micTarget=0,this.strand.silence(),this.strand.write(this.stringData,this.limb.length),this.stringRev++,this.stringMoved=!1,this.restMix=+!!this.resting,this.glow=1-.5800000000000001*this.restMix,this.sweep=-1e4,this.aurora=+(this.state===`thinking`),this.auroraDrift=0,this.figureClock=this.resting?1e3:0,this.figureAlpha=+!!this.resting,this.skyOpen=+!!this.sky,this.bumps=[],this.meteors=[],this.recalls=[],this.parallax=[0,0],this.shift=[0,0],this.writing&&(this.writing.fadeAt=1/0,this.writing.t0=this.time-this.writing.duration-5)}endWriting(){this.writing=null}active(){let e=this.time,t=this.writing;return!this.strand.still||this.aurora>.004||this.state===`thinking`||this.state===`loading`||this.meteors.length>0||this.recalls.length>0||this.bumps.length>0||this.wipeFront<this.wipeEnd||e-this.errorAt<3||t!==null&&(e-t.t0<t.duration+1||e>t.fadeAt)}get colours(){let{glow:e,upper:t}=this.palette;return{a:e[this.from],b:e[this.to],upA:t[this.from],upB:t[this.to]}}get stringLive(){return!this.strand.still||this.stringMoved}backgroundStill(){return!this.stringLive&&this.bumps.length===0&&this.mic<.002&&this.voice<.002&&this.sweep<-1e3&&this.wipeFront===1/0&&this.aurora<=.004}get bumpCount(){return this.bumps.length}bumpsForShader(){let e=this.bumpData;e.fill(0),this.bumps.forEach((t,n)=>{e[n*4]=t.s,e[n*4+1]=t.sigma,e[n*4+2]=t.amp*Math.exp(-(this.time-t.t0)/t.tau)});for(let t=this.bumps.length;t<4;t++)e[t*4+1]=1;return e}recallsForShader(e){let t=this.sky;if(!t)return 0;let n=0;for(let r of this.recalls){let i=this.time-r.t0;if(i<0||n>=8)continue;let a=20*(i<Ie?i/Ie:Math.exp(-(i-Ie)/1));a<.02||(e.set(t.dirs.subarray(r.star*3,r.star*3+3),n*8),e[n*8+3]=t.vmag[r.star],e.set(this.starColors.subarray(r.star*3,r.star*3+3),n*8+4),e[n*8+7]=a,n++)}return n}meteorsForShader(e){let t=0;for(let n of this.meteors){if(t>=8)break;e.set([n.ox,n.oy,n.tx,n.ty,this.time-n.t0,n.dur,n.colour[0],n.colour[1],n.colour[2]],t*9),t++}return t}writeGain(){let e=this.writing;return e?(this.time>e.fadeAt?1-He((this.time-e.fadeAt)/Z):1)*(e.pulse&&this.time-e.t0>e.duration?1+.12*Math.sin(2*Math.PI*.8*(this.time-e.t0)):1):0}get flicker(){return this.time%1024}get patch(){return this.time/80%1}};const He=e=>{let t=Math.min(1,Math.max(0,e));return t*t*(3-2*t)},Ue=(e,t)=>[e[0]+(1-e[0])*t,e[1]+(1-e[1])*t,e[2]+(1-e[2])*t],Q=`#version 300 es
precision highp float;
precision highp int;
precision highp sampler2D;
const float PI=3.14159265;
const float TAU=6.28318531;
`,We=`vec3 encode(vec3 c){
c=clamp(c,0.0,1.0);
if(max(c.r,max(c.g,c.b))<0.0031308)return c*12.92;
return mix(c*12.92,1.055*pow(c,vec3(1.0/2.4))- 0.055,step(vec3(0.0031308),c));
}
uint pcg(uint v){
uint s=v*747796405u + 2891336453u;
uint w=((s>>((s>>28u)+ 4u))^ s)*277803737u;
return(w>>22u)^ w;
}
float rnd(uint v){return float(pcg(v)>>8u)*(1.0/16777216.0);}
float dither(vec2 p){
float a=fract(52.9829189*fract(dot(p,vec2(0.06711056,0.00583715))));
float b=fract(52.9829189*fract(dot(p + vec2(5.3,11.7),vec2(0.06711056,0.00583715))));
return(a + b - 1.0)*(1.0/255.0);
}
float cdf(float x){return 0.5 + 0.5*tanh(clamp(0.7978845608*(x + 0.044715*x*x*x),-12.0,12.0));}
float profile(float a,float s,float w){return(cdf((a + 0.5*w)/s)- cdf((a - 0.5*w)/s))/w;}
`,$=`uniform vec2 uCss;
uniform vec2 uPx;
uniform vec4 uLimb;
float altitude(vec2 p){
float dx=p.x - uLimb.x;
float e=p.y - uLimb.w;
float d=length(vec2(dx,uLimb.z - e));
return(dx*dx + e*(e - 2.0*uLimb.z))/(d + uLimb.z);
}
float arc(vec2 p){return uLimb.z*atan(p.x - uLimb.x,uLimb.y - p.y);}
vec4 clipPos(vec2 p){return vec4(p.x/uCss.x*2.0 - 1.0,1.0 - p.y/uCss.y*2.0,0.0,1.0);}
`,Ge=`vec2 cssPos(){return vec2(gl_FragCoord.x/uPx.x,uCss.y - gl_FragCoord.y/uPx.y);}
`,Ke=`uniform sampler2D uString;
uniform vec2 uSpan;
vec3 stringAt(float s){
int n=textureSize(uString,0).x;
float x=clamp((s - uSpan.x)*uSpan.y,0.0,1.0)*float(n - 1);
int i=int(floor(x));
float f=x - float(i);
vec3 a=texelFetch(uString,ivec2(i,0),0).xyz;
vec3 b=texelFetch(uString,ivec2(min(i + 1,n - 1),0),0).xyz;
return mix(a,b,f);
}
`,qe=`uniform mat3 uViewInv;
uniform vec4 uFrame;
uniform vec2 uShift;
vec2 place(vec3 q){
float k=2.0/(1.0 + q.z);
return vec2(uFrame.y + q.x*k*uFrame.x,uFrame.z - q.y*k*uFrame.x)+ uShift;
}
`,Je=`uniform sampler2D uNoise;
uniform sampler2D uMilkyWay;
uniform vec4 uMW;
uniform vec4 uGlow;
uniform vec3 uColB;
uniform vec3 uUpB;
uniform vec3 uSky;
uniform vec3 uNebula;
uniform vec3 uDust;
uniform vec4 uM42;
${qe}
vec4 noise(vec2 uv){return textureLod(uNoise,uv,0.0);}
vec3 skyAt(vec2 p,float a){
float h=1.0 - clamp(a/max(uLimb.w,1.0),0.0,1.0);
vec3 lin=uSky + mix(uColB,uUpB,0.6)*(uGlow.x*0.0011*h*h*sqrt(h));
if(a<300.0)lin +=uUpB*(uGlow.x*0.0085*exp(-a/62.0));
if(uFrame.w>0.0){
vec2 ps=p - uShift;
lin +=uDust*(uMW.w*uFrame.w*textureLod(uMilkyWay,(ps + uMW.x)*uMW.yz,0.0).r);
vec2 pl=vec2(ps.x - uFrame.y,uFrame.z - ps.y)/uFrame.x;
vec2 dn=(pl - uM42.xy)/uM42.zw;
float d2=dot(dn,dn);
if(d2<16.0)lin +=uNebula*(0.06*exp(-0.5*d2)*(0.45 + noise(pl*7.0 + 0.3).a)*uFrame.w);
}
return lin;
}
`,Ye=`${Q}${$}
uniform vec4 uBand;
out float vA;
out float vS;
out vec2 vN;
void main(){
int cell=gl_VertexID>>1;
float a=(gl_VertexID&1)==0?uBand.x:uBand.y;
float theta=mix(-uBand.z,uBand.z,float(cell)/uBand.w);
vN=vec2(sin(theta),-cos(theta));
vA=a;
vS=uLimb.z*theta;
gl_Position=clipPos(uLimb.xy + vN*(uLimb.z + a));
}
`,Xe=`${Q}${We}${$}${Ge}${Je}
in float vA;
out vec4 outColor;
void main(){
outColor=vec4(encode(skyAt(cssPos(),vA))+ dither(gl_FragCoord.xy),1.0);
}
`,Ze=`${Q}${We}${$}${Ge}${Ke}${Je}
uniform sampler2D uAurora;
uniform vec2 uAurSeen;
uniform float uPatch;
uniform vec3 uColA;
uniform vec3 uUpA;
uniform vec3 uWipe;
uniform vec4 uBump[4];
uniform vec4 uLive;
uniform vec3 uGround;
in float vA;
in float vS;
in vec2 vN;
out vec4 outColor;
void main(){
vec2 p=cssPos();
float a=vA;
float s=vS;
vec3 str=uLive.x>0.5?stringAt(s):vec3(0.0);
float ad=a - str.x;
vec3 core=uColB;
vec3 up=uUpB;
if(uWipe.y<1.0e5){
float m=1.0 - smoothstep(uWipe.y - uWipe.z,uWipe.y,abs(s - uWipe.x));
core=mix(uColA,uColB,m);
up=mix(uUpA,uUpB,m);
}
float along=1.0 + 0.34*(noise(vec2(s/1700.0 + uPatch,0.31)).a - 0.5)+ 0.05*sin(TAU*uPatch*4.0);
along*=0.82 + 0.5*uGlow.y;
if(uLive.z>0.5){
float dm=(s - uWipe.x)/(0.22*uCss.x);
along*=1.0 + 0.9*uGlow.z*exp(-dm*dm);
}
for(int i=0;i<int(uLive.y);i++){
float db=(s - uBump[i].x)/uBump[i].y;
along +=uBump[i].z*exp(-0.5*db*db);
}
if(uLive.w>0.5){
float dw=(s - uGlow.w)/(0.06*uCss.x);
along +=1.4*exp(-0.5*dw*dw);
}
float light=uGlow.x*along;
vec3 lin;
if(a<0.0){
lin=uGround;
float depth=max(-ad,0.0);
if(depth<70.0){
float z=1.0/(depth/70.0 + 0.5);
float cl=noise(vec2(s/1500.0*z*0.22 + uPatch,z*0.07)).a;
lin +=light*0.045*exp(-depth/15.0)*(0.35 + 1.15*cl)*mix(core,up,0.35);
}
}else{
lin=skyAt(p,a);
}
if(a>-40.0&&a<250.0){
float sig=0.55;
float sg=sqrt(sig*sig + str.z*str.z/3.0);
float d=ad*inversesqrt(1.0 + str.y*str.y);
float above=max(a,0.0);
float w=dot(abs(vN),1.0/uPx);
float atmosphere=above<120.0?0.14*exp(-above/3.2)+ 0.06*exp(-above/15.0):0.0;
vec3 glow=atmosphere*clamp(a/w + 0.5,0.0,1.0)*mix(core,up,smoothstep(0.0,70.0,above));
if(abs(d)<4.0*sg + w)glow +=profile(d,sg,w)*2.5066*sig*mix(core,vec3(1.0),0.42);
if(abs(d)<40.0)glow +=0.2*exp(-abs(d)/3.2)*core;
lin +=light*glow;
}
if(uAurSeen.x>0.5&&ad>-2.0&&ad<uAurSeen.y + 8.0)
lin +=textureLod(uAurora,gl_FragCoord.xy/(uCss*uPx),0.0).rgb;
outColor=vec4(encode(lin)+ dither(gl_FragCoord.xy),1.0);
}
`,Qe=`${Q}${We}${$}${Ke}
uniform sampler2D uNoise;
uniform sampler2D uRays;
uniform vec4 uAur;
uniform vec4 uBump[4];
uniform vec4 uLive;
uniform vec3 uAuLow;
uniform vec3 uAuHigh;
uniform float uTime;
in float vA;
in float vS;
out vec4 outColor;
vec4 noise(vec2 uv){return textureLod(uNoise,uv,0.0);}
vec3 sheet(float s,float ad,float k,float amount,float fl){
float W=uCss.x;
float dr=uAur.w;
float cover=smoothstep(0.40,0.62,noise(vec2((s + dr*0.35)/(1.35*W)+ 0.173*k,0.5 + 0.31*k)).g);
if(cover<=0.0)return vec3(0.0);
vec2 fuv=vec2((s + dr*0.55)/(0.5*W)+ 0.41*k,dr*0.0006 + 0.7*k);
float f0=noise(fuv).r;
float f1=noise(fuv + vec2(0.004,0.0)).r;
float fold=(f0 - 0.5)*0.12*W;
float slope=(f1 - f0)*0.12*W/(0.004*0.5*W);
float edge=0.45 + 0.85*clamp(abs(slope),0.0,2.2);
float q=s + fold + dr*0.8 + ad*(f0 - 0.5)*0.3;
vec4 r=textureLod(uRays,vec2(q/3276.8 + 0.37*k,0.5),0.0);
float rays=clamp(r.r*0.75 + r.g*0.5 - 0.2,0.0,1.0);
if(rays<=0.0)return vec3(0.0);
rays*=rays*(3.0 - 2.0*rays);
float H=uAur.y*(0.22 + 0.78*r.b*r.b)*(0.55 + 0.45*amount)*(1.0 + 0.7*fl)/1.7;
if(ad>H)return vec3(0.0);
float low=1.5 + 3.5*r.a;
float prof=smoothstep(low - 1.2,low + 2.5,ad)*exp(-max(ad - low,0.0)/(0.4*H))
*(1.0 - smoothstep(0.6*H,H,ad));
float shim=0.7 + 0.3*sin(TAU*uTime*(1.1 + 0.6*r.b)- ad*0.05 + 6.0*r.b);
vec3 col=mix(uAuLow,uAuHigh,smoothstep(0.0,0.8*H,ad))*(1.0 + 1.5*exp(-ad/7.0));
return col*(cover*rays*prof*shim*edge*amount*(1.0 + 1.1*fl)*0.52);
}
void main(){
float s=vS;
float ad=vA - stringAt(s).x;
vec3 sum=vec3(0.0);
if(ad>0.0&&ad<uAur.y){
float fl=0.0;
for(int i=0;i<int(uLive.y);i++){
float db=(s - uBump[i].x)/(uBump[i].y*1.6);
fl +=uBump[i].z*exp(-0.5*db*db);
}
fl=min(fl,1.0);
sum=sheet(s,ad,0.0,uAur.x,fl);
if(uAur.z>1.5)sum +=0.6*sheet(s,ad,1.0,uAur.x,fl);
}
outColor=vec4(sum,1.0);
}
`,$e=`${Q}${We}${$}${qe}
layout(location=0)in vec3 aDir;
layout(location=1)in float aMag;
layout(location=2)in vec3 aCol;
layout(location=3)in float aBoost;
uniform vec4 uStar;
uniform float uCut;
uniform float uTime;
uniform float uTwinkle;
flat out vec3 vLight;
flat out vec2 vPos;
void main(){
vec3 q=aDir*uViewInv;
vec2 corner=vec2(float(gl_VertexID&1),float(gl_VertexID>>1))*2.0 - 1.0;
gl_Position=vec4(2.0,2.0,2.0,1.0);
if(q.z<-0.995)return;
vec2 pos=place(q);
float alt=altitude(pos);
uint id=uint(gl_InstanceID);
float low=exp(-max(alt,0.0)/(0.14*uLimb.w));
float h0=rnd(id*3u);
float h1=rnd(id*3u + 1u);
float h2=rnd(id*3u + 2u);
float n1=sin(uTime*(4.1 + 3.7*h0)+ h1*TAU)*sin(uTime*(1.7 + 2.1*h2)+ h2*TAU);
float n2=sin(uTime*(3.3 + 2.9*h1)+ h0*TAU)*sin(uTime*(2.3 + 1.9*h0)+ h1*TAU);
float amp=(0.035 + 0.5*low)*uTwinkle;
vec3 tint=vec3(1.0 + 0.55*amp*n2,1.0,1.0 - 0.55*amp*n2);
vec3 ext=exp(-vec3(1.0,1.9,3.9)*1.7*exp(-max(alt,0.0)/14.0));
float flux=pow(10.0,-0.4*(aMag - 6.5))*(1.0 + aBoost);
vec3 light=aCol*tint*ext*(uStar.y*flux*(1.0 + amp*n1));
float peak=max(light.r,max(light.g,light.b));
if(peak<uCut)return;
float core=uStar.x*sqrt(2.0*log(max(1.0001,peak/uCut)));
float wing=peak*uStar.z;
float wings=wing>uCut?uStar.w*sqrt(pow(wing/uCut,2.0/3.0)- 1.0):0.0;
float reach=max(core,wings)+ 1.0;
if(alt<-reach||pos.x<-reach||pos.x>uCss.x + reach||pos.y<-reach)return;
vLight=light;
vPos=pos;
gl_Position=clipPos(pos + corner*reach);
}
`,et=`${Q}${We}${$}${Ge}
uniform vec4 uStar;
flat in vec3 vLight;
flat in vec2 vPos;
out vec4 outColor;
void main(){
vec2 p=cssPos();
vec2 d=p - vPos;
vec2 hw=0.5/uPx;
float sg=uStar.x;
float dens=(cdf((d.x + hw.x)/sg)- cdf((d.x - hw.x)/sg))*(cdf((d.y + hw.y)/sg)- cdf((d.y - hw.y)/sg))
*(2.0*PI*sg*sg)*(uPx.x*uPx.y);
float wing=uStar.z/pow(1.0 + dot(d,d)/(uStar.w*uStar.w),1.5);
vec3 shown=1.0 - exp(-vLight*(dens + wing));
float cover=clamp(altitude(p)*0.5*(uPx.x + uPx.y)+ 0.5,0.0,1.0);
outColor=vec4(encode(shown*cover),1.0);
}
`,tt=`uniform float uMargin;
flat out vec4 vSeg;
flat out vec4 vPen;
out vec2 vP;
void quad(vec2 p0,vec2 p1,vec4 pen){
vec2 d=p1 - p0;
float len=length(d);
vec2 t=len>1e-4?d/len:vec2(1.0,0.0);
vec2 n=vec2(-t.y,t.x);
vec2 c=vec2(float(gl_VertexID&1),float(gl_VertexID>>1))*2.0 - 1.0;
vec2 pos=mix(p0,p1,c.x*0.5 + 0.5)+ t*(c.x*uMargin)+ n*(c.y*uMargin);
vSeg=vec4(p0,p1);
vPen=pen;
vP=pos;
gl_Position=clipPos(pos);
}
`,nt=`${Q}${$}${tt}
layout(location=0)in vec4 aSeg;
layout(location=1)in vec4 aPen;
uniform vec2 uOrigin;
void main(){quad(aSeg.xy + uOrigin,aSeg.zw + uOrigin,aPen);}
`,rt=`${Q}${$}${qe}${tt}
layout(location=0)in vec3 aA;
layout(location=1)in vec3 aB;
layout(location=2)in vec2 aTime;
uniform float uGap;
void main(){
vec2 p0=place(aA*uViewInv);
vec2 p1=place(aB*uViewInv);
vec2 t=normalize(p1 - p0 + 1e-4);
quad(p0 + t*uGap,p1 - t*uGap,vec4(aTime.x,aTime.x + aTime.y,1.0,1.0));
}
`,it=`${Q}${We}
uniform vec2 uPx;
uniform float uT;
uniform vec3 uColour;
uniform vec4 uStroke;
uniform vec3 uInk;
flat in vec4 vSeg;
flat in vec4 vPen;
in vec2 vP;
out vec4 outColor;
void main(){
float t0=vPen.x;
float t1=vPen.y;
if(uT<t0)discard;
float f=clamp((uT - t0)/max(t1 - t0,1e-4),0.0,1.0);
vec2 p0=vSeg.xy;
vec2 d=vSeg.zw - p0;
float dd=dot(d,d);
float h=dd>1e-6?clamp(dot(vP - p0,d)/dd,0.0,f):0.0;
float r=length(vP -(p0 + d*h));
float age=max(uT -(t0 + h*(t1 - t0)),0.0);
float ink=mix(vPen.z,vPen.w,h);
float w=0.5*(1.0/uPx.x + 1.0/uPx.y);
float core=profile(r,uStroke.x,w)*2.5066*uStroke.x;
float halo=uStroke.y*exp(-r/uStroke.z);
float wet=1.0 + uInk.x*exp(-age/uInk.y);
vec2 tipAt=vP -(p0 + d*f);
float tip=uT<=t1 + 0.02?uInk.z*exp(-0.5*dot(tipAt,tipAt)/6.76):0.0;
outColor=vec4(encode(uColour*((ink*wet*(core + halo)+ tip)*uStroke.w)),1.0);
}
`,at=`${Q}${$}
layout(location=0)in vec4 aPath;
layout(location=1)in vec2 aAge;
layout(location=2)in vec3 aColour;
uniform vec2 uTrail;
const int N=36;
out float vAcross;
out float vTau;
out float vHead;
out float vFade;
out vec3 vColour;
float ease(float x){return 1.0 - pow(1.0 - clamp(x,0.0,1.0),2.4);}
void main(){
int i=gl_VertexID>>1;
float side=(gl_VertexID&1)==0?-1.0:1.0;
float tau=float(i)/float(N);
float age=aAge.x;
float D=aAge.y;
float tm=clamp(age - tau*uTrail.x,0.0,D);
vec2 pos=mix(aPath.xy,aPath.zw,ease(tm/D));
vec2 head=mix(aPath.xy,aPath.zw,ease(age/D));
vec2 t=normalize(aPath.zw - aPath.xy + 1e-4);
if(i==0)pos +=t*7.0;
float width=4.5*(1.0 - 0.5*tau);
vAcross=side*width;
vTau=tau;
vHead=length(pos - head);
vFade=1.0 - smoothstep(D,D + uTrail.y,age);
vColour=aColour;
gl_Position=clipPos(pos + vec2(-t.y,t.x)*(side*width));
}
`,ot=`${Q}${We}
in float vAcross;
in float vTau;
in float vHead;
in float vFade;
in vec3 vColour;
out vec4 outColor;
void main(){
float x=vAcross;
float body=exp(-x*x/0.5)+ 0.28*exp(-abs(x)/1.4);
float head=1.6*exp(-0.5*vHead*vHead/4.84)+ 0.25*exp(-vHead/9.0);
float light=(body*pow(1.0 - vTau,1.7)+ head*exp(-x*x/6.0))*vFade;
outColor=vec4(encode(mix(vColour,vec3(1.0),0.65*(1.0 - vTau))*light),1.0);
}
`;function st(e,t){let n=T(e,Ye,Xe),r=T(e,Ye,Ze),i=T(e,Ye,Qe),a=T(e,$e,et),o=T(e,rt,it),s=T(e,nt,it),c=T(e,at,ot),l=t.ground.map(e=>e<=.0031308?12.92*e:1.055*e**(1/2.4)-.055),u=()=>{let t=e.createBuffer();if(!t)throw Error(`no buffer`);return t},d=()=>{let t=e.createVertexArray();if(!t)throw Error(`no vertex array`);return t},f=(t,n,r)=>{e.bindBuffer(e.ARRAY_BUFFER,t);for(let[t,i,a]of r)e.enableVertexAttribArray(t),e.vertexAttribPointer(t,i,e.FLOAT,!1,n*4,a*4),e.vertexAttribDivisor(t,1)},p=u(),m=d();e.bindVertexArray(m),f(p,7,[[0,3,0],[1,1,3],[2,3,4]]),e.bindVertexArray(null);let h=u(),g=d();e.bindVertexArray(g),f(h,8,[[0,3,0],[1,1,3],[2,3,4],[3,1,7]]),e.bindVertexArray(null),e.bindBuffer(e.ARRAY_BUFFER,h),e.bufferData(e.ARRAY_BUFFER,256,e.DYNAMIC_DRAW);let _=u(),v=d();e.bindVertexArray(v),f(_,8,[[0,3,0],[1,3,3],[2,2,6]]),e.bindVertexArray(null);let y=u(),b=d();e.bindVertexArray(b),f(y,8,[[0,4,0],[1,4,4]]),e.bindVertexArray(null);let x=u(),S=d();e.bindVertexArray(S),f(x,9,[[0,4,0],[1,2,4],[2,3,6]]),e.bindVertexArray(null),e.bindBuffer(e.ARRAY_BUFFER,x),e.bufferData(e.ARRAY_BUFFER,288,e.DYNAMIC_DRAW),e.vertexAttrib1f(3,0),e.pixelStorei(e.UNPACK_ALIGNMENT,1);let ee={internal:e.RGBA8,format:e.RGBA,type:e.UNSIGNED_BYTE},C={internal:e.R8,format:e.RED,type:e.UNSIGNED_BYTE},w=se(),te=E(e,256,256,ee,e.LINEAR,w,e.REPEAT),ne=E(e,j,1,ee,e.LINEAR,ce(),e.REPEAT),re=E(e,1,1,C,e.LINEAR,/* @__PURE__ */ new Uint8Array(1)),ie=null,ae=``,O=E(e,512,1,{internal:e.RGBA32F,format:e.RGBA,type:e.FLOAT},e.NEAREST,null),oe=-1,k=1,A=1,M=1,N=1,P=null,le=0,ue=0,F=-1,I=-1,de=0,L=null,fe=-1,R=-1,z=-1/0,pe=/* @__PURE__ */ new Float32Array(64),B=/* @__PURE__ */ new Float32Array(72),V=t=>{if(!t.sky)return;let n=`${t.width}|${t.height}|${t.frame.scale}|${t.frame.x}|${t.frame.y}`;n!==ae&&(ae=n,ie=ye(t.width,t.height,t.frame,t.view,w,256),W++,e.deleteTexture(re),re=E(e,ie.w,ie.h,C,e.LINEAR,ie.data))},H={target:null,sig:null,used:-1/0},U={target:null,sig:null,used:-1/0},W=0,G=t=>{t.target&&(e.deleteFramebuffer(t.target.fbo),e.deleteTexture(t.target.tex)),t.target=null,t.sig=null},me=(e,t)=>{let n=e.limb,r=e.colours;return[k,A,M,N,n.cx,n.cy,n.r,n.horizon,e.frame.scale,e.frame.x,e.frame.y,e.skyOpen,+!!e.sky,W,...e.m42,e.glow,...lt(e),...r.b,...r.upB,t]},he=(e,t)=>{let n=e.colours;return[...me(e,t),e.limb.length,e.talkArc,ct(e.patch,256),e.auroraTop,...n.a,...n.upA]},ge=(t,n,r)=>{if(!t.target){let n=D(e,k,A,ee,e.NEAREST);t.target={fbo:n.fbo,tex:n.tex[0],w:k,h:A}}let i=Float32Array.from(n),a=t.sig;return(!a||a.length!==i.length||i.some((e,t)=>e!==a[t]))&&(e.bindFramebuffer(e.FRAMEBUFFER,t.target.fbo),e.viewport(0,0,k,A),r(),e.bindFramebuffer(e.FRAMEBUFFER,null),t.sig=i),t.target},_e=(t,n)=>{e.bindFramebuffer(e.READ_FRAMEBUFFER,t.fbo),e.bindFramebuffer(e.DRAW_FRAMEBUFFER,n),e.blitFramebuffer(0,0,k,A,0,0,k,A,e.COLOR_BUFFER_BIT,e.NEAREST),e.bindFramebuffer(e.FRAMEBUFFER,null)},ve=()=>{L&&=(e.deleteFramebuffer(L.fbo),e.deleteTexture(L.tex),null)},be=t=>{let n=Math.min(P.aurora,k/M,A/N),r=Math.max(1,Math.round(M*n)),i=Math.max(1,Math.round(N*n)),{horizon:a,r:o}=t.limb;if(L&&L.w===r&&L.h===i&&fe===a&&R===o)return L;ve();let s=D(e,r,i,{internal:e.SRGB8_ALPHA8,format:e.RGBA,type:e.UNSIGNED_BYTE},e.LINEAR);return L={fbo:s.fbo,tex:s.tex[0],w:r,h:i},fe=a,R=o,e.bindFramebuffer(e.FRAMEBUFFER,s.fbo),e.clearColor(0,0,0,1),e.clear(e.COLOR_BUFFER_BIT),L},xe=t=>{let n=t.sky;if(!n||ue===n.count)return;let r=new Float32Array(n.count*7);for(let e=0;e<n.count;e++)r.set(n.dirs.subarray(e*3,e*3+3),e*7),r[e*7+3]=n.vmag[e],r.set(t.starColors.subarray(e*3,e*3+3),e*7+4);e.bindBuffer(e.ARRAY_BUFFER,p),e.bufferData(e.ARRAY_BUFFER,r,e.STATIC_DRAW),ue=n.count,Se(t),F=-1},Se=e=>{let t=e.sky;if(!t||!P)return;let n=0,r=t.count;for(;n<r;){let e=n+r>>1;t.vmag[e]<=P.faintest?n=e+1:r=e}le=n},K=t=>{let n=t.sky;if(!n||F===t.figureRev)return;let r=n.lines.length/2,i=new Float32Array(r*8);for(let e=0;e<r;e++){let r=n.lines[e*2],a=n.lines[e*2+1];i.set(n.dirs.subarray(r*3,r*3+3),e*8),i.set(n.dirs.subarray(a*3,a*3+3),e*8+3),i[e*8+6]=t.figureTimes[e*2],i[e*8+7]=t.figureTimes[e*2+1]}e.bindBuffer(e.ARRAY_BUFFER,_),e.bufferData(e.ARRAY_BUFFER,i,e.STATIC_DRAW),F=t.figureRev},Ce=t=>{let n=t.writing;if(!n||I===n.rev)return;let r=new Float32Array(n.segments.length*8);n.segments.forEach((e,t)=>{r.set([e.x0,e.y0,e.x1,e.y1,e.t0,e.t1,e.ink0,e.ink1],t*8)}),e.bindBuffer(e.ARRAY_BUFFER,y),e.bufferData(e.ARRAY_BUFFER,r,e.DYNAMIC_DRAW),de=n.segments.length,I=n.rev},q=(t,n,r,i)=>{let{u:a}=t,o=n.limb;e.uniform2f(a.uCss,n.width,n.height),e.uniform2f(a.uPx,r,i),e.uniform4f(a.uLimb,o.cx,o.cy,o.r,o.horizon),e.uniform2f(a.uSpan,o.start,1/o.length)},we=(t,n,r)=>{let{u:i}=t;e.uniformMatrix3fv(i.uViewInv,!1,n.view),e.uniform4f(i.uFrame,n.frame.scale,n.frame.x,n.frame.y,r),e.uniform2f(i.uShift,n.shift[0],n.shift[1])},J=(t,n)=>{e.activeTexture(e.TEXTURE0+t),e.bindTexture(e.TEXTURE_2D,n)},Y=(t,n)=>e.uniform3f(t,n[0],n[1],n[2]),X=t=>e.useProgram(t.program),Te=(t,n,r,i)=>{let a=n.limb,o=Math.asin(Math.min(1,(M/2+40)/(a.r+Math.min(r,0))));e.uniform4f(t.u.uBand,r,i,o,128),e.drawArrays(e.TRIANGLE_STRIP,0,258)},Ee=n=>{let r=P,a=be(n);e.bindFramebuffer(e.FRAMEBUFFER,a.fbo),e.viewport(0,0,a.w,a.h),X(i);let{u:o}=i;q(i,n,a.w/M,a.h/N),J(0,O),J(1,te),J(4,ne),e.uniform1i(o.uString,0),e.uniform1i(o.uNoise,1),e.uniform1i(o.uRays,4),e.uniform4f(o.uLive,0,n.bumpCount,0,0),e.uniform4f(o.uAur,n.aurora,n.auroraTop,r.sheets,n.auroraDrift),e.uniform4fv(o.uBump,n.bumpsForShader()),Y(o.uAuLow,t.auroraLow),Y(o.uAuHigh,t.auroraHigh),e.uniform1f(o.uTime,n.flicker),Te(i,n,-30,n.auroraTop),e.bindFramebuffer(e.FRAMEBUFFER,null)},De=(n,r)=>{let{u:i}=n;q(n,r,k/M,A/N),we(n,r,r.sky?ut(r.skyOpen):0);let[a,o]=lt(r);e.uniform2f(i.uShift,a,o),J(1,te),J(3,re),e.uniform1i(i.uNoise,1),e.uniform1i(i.uMilkyWay,3),ie&&e.uniform4f(i.uMW,32,1/(M+64),1/(N+64),.0053),e.uniform4f(i.uGlow,r.glow,r.voice,r.mic,r.sweep);let s=r.colours;Y(i.uColB,s.b),Y(i.uUpB,s.upB),Y(i.uSky,t.sky),Y(i.uNebula,t.nebula),Y(i.uDust,t.dust),e.uniform4f(i.uM42,r.m42[0],r.m42[1],r.m42[2],r.m42[3])},Oe=(t,r)=>{e.clearColor(l[0],l[1],l[2],1),e.clear(e.COLOR_BUFFER_BIT);let i=t.limb.horizon+8;i>r&&(X(n),De(n,t),Te(n,t,r,i))},ke=(n,i,a)=>{X(r);let{u:o}=r;De(r,n),J(0,O),e.uniform1i(o.uString,0),e.uniform4f(o.uLive,+!!n.stringLive,n.bumpCount,+(n.mic>.002),+(n.sweep>-1e3)),i&&L&&(J(2,L.tex),e.uniform1i(o.uAurora,2)),e.uniform2f(o.uAurSeen,+!!i,n.auroraTop),e.uniform1f(o.uPatch,ct(n.patch,256));let s=n.colours;Y(o.uColA,s.a),Y(o.uUpA,s.upA),e.uniform3f(o.uWipe,n.talkArc,Math.min(n.wipeFront,1e6),160),e.uniform4fv(o.uBump,n.bumpsForShader()),Y(o.uGround,t.ground),Te(r,n,-80,Math.min(a,n.limb.horizon+8))},Ae=(t,n,r)=>{let i=n?Math.max(t.auroraTop+8,220):220;if(!r){Oe(t,i),ke(t,n,i);return}H.used=t.time;let a=ge(H,me(t,i),()=>Oe(t,i));if(n||!t.backgroundStill()){_e(a,null),ke(t,n,i);return}U.used=t.time;let o=ge(U,he(t,i),()=>{_e(a,U.target.fbo),e.bindFramebuffer(e.FRAMEBUFFER,U.target.fbo),ke(t,!1,i)});_e(o,null)},je=(t,n)=>{let r=ut(t.skyOpen);if(!t.sky||r<=0)return;e.enable(e.BLEND),e.blendEquation(e.FUNC_ADD),e.blendFunc(e.ONE,e.ONE_MINUS_SRC_COLOR),X(a);let{u:i}=a;q(a,t,k/M,A/N),we(a,t,r),e.uniform4f(i.uStar,.55,.018*r,.035,1.7),e.uniform1f(i.uCut,6e-4),e.uniform1f(i.uTime,t.flicker),e.uniform1f(i.uTwinkle,+!!n),e.bindVertexArray(m),e.drawArraysInstanced(e.TRIANGLE_STRIP,0,4,le);let o=t.recallsForShader(pe);o>0&&(e.bindBuffer(e.ARRAY_BUFFER,h),e.bufferSubData(e.ARRAY_BUFFER,0,pe,0,o*8),e.bindVertexArray(g),e.drawArraysInstanced(e.TRIANGLE_STRIP,0,4,o)),e.bindVertexArray(null)},Me=(t,n,r,i,a)=>{let{u:o}=t;e.uniform1f(o.uT,n),Y(o.uColour,r),e.uniform4f(o.uStroke,i[0],i[1],i[2],i[3]),e.uniform3f(o.uInk,a[0],a[1],a[2])},Ne=n=>{let r=n.sky;if(!r||n.figureAlpha<.003||n.skyOpen<=0)return;K(n),X(o);let{u:i}=o;q(o,n,k/M,A/N),we(o,n,1),e.uniform1f(i.uMargin,6),e.uniform1f(i.uGap,7),Me(o,n.figureClock,t.figure,[.5,0,1,.38*n.figureAlpha],[0,1,.9]),e.bindVertexArray(v),e.drawArraysInstanced(e.TRIANGLE_STRIP,0,4,r.lines.length/2),e.bindVertexArray(null)},Pe=t=>{let n=t.writing,r=t.writeGain();if(!n||r<=0)return;Ce(t),X(s);let{u:i}=s;q(s,t,k/M,A/N),e.uniform1f(i.uMargin,16),e.uniform2f(i.uOrigin,n.origin[0],n.origin[1]),Me(s,t.time-n.t0,n.colour,[.8,.08,3.2,r],[1.2,.35,2.2]),e.bindVertexArray(b),e.drawArraysInstanced(e.TRIANGLE_STRIP,0,4,de),e.bindVertexArray(null)},Ie=t=>{let n=t.meteorsForShader(B);if(n===0)return;e.enable(e.BLEND),e.blendEquation(e.FUNC_ADD),e.blendFunc(e.ONE,e.ONE_MINUS_SRC_COLOR),X(c);let{u:r}=c;q(c,t,k/M,A/N),e.uniform2f(r.uTrail,.26,Fe),e.bindBuffer(e.ARRAY_BUFFER,x),e.bufferSubData(e.ARRAY_BUFFER,0,B,0,n*9),e.bindVertexArray(S),e.drawArraysInstanced(e.TRIANGLE_STRIP,0,74,n),e.bindVertexArray(null)},Le=null,Z=e=>Le?.(e);return{get profile(){return Le},set profile(e){Le=e},size(e,t,n,r){k=e,A=t,M=n,N=r,ve(),G(H),G(U)},tier(e){let t=P?.aurora!==e.aurora;P=e,t&&ve()},draw(t,n){if(e.viewport(0,0,k,A),e.disable(e.BLEND),!t.laid||!P){e.clearColor(0,0,0,1),e.clear(e.COLOR_BUFFER_BIT);return}xe(t),V(t),Se(t),t.stringRev!==oe&&(J(0,O),e.texSubImage2D(e.TEXTURE_2D,0,0,0,512,1,e.RGBA,e.FLOAT,t.stringData),oe=t.stringRev);let r=t.aurora>.004;r?(z=t.time,Z(`aurora`),Ee(t)):L&&t.time-z>20&&ve();for(let e of[H,U])e.target&&t.time-e.used>60&&G(e);e.viewport(0,0,k,A),Z(`background`),Ae(t,r,n),Z(`stars`),je(t,n),e.disable(e.BLEND),(t.figureAlpha>=.003||t.writing)&&(Z(`strokes`),e.enable(e.BLEND),e.blendEquation(e.MAX),e.blendFunc(e.ONE,e.ONE),Ne(t),Pe(t),e.blendEquation(e.FUNC_ADD)),Z(`meteors`),Ie(t),e.disable(e.BLEND),Z(``)},dispose(){ve(),G(H),G(U);for(let t of[n,r,i,a,o,s,c])e.deleteProgram(t.program);for(let t of[p,h,_,y,x])e.deleteBuffer(t);for(let t of[m,g,v,b,S])e.deleteVertexArray(t);e.deleteTexture(te),e.deleteTexture(ne),e.deleteTexture(re),e.deleteTexture(O)}}}const ct=(e,t)=>Math.floor(e*t)/t,lt=e=>[Math.round(e.shift[0]*4)/4,Math.round(e.shift[1]*4)/4],ut=e=>{let t=Math.min(1,Math.max(0,e));return t*t*(3-2*t)};function dt(e,t){let n=Math.max(0,Math.sin(e*Math.PI*2*4.3))**1.5,r=.55+.45*Math.sin(e*Math.PI*2*.37),i=Math.sin(e*Math.PI*2*.21)>-.7?1:.08,a=Math.min(1,(.15+.85*n)*r*i);for(let n=0;n<t.length;n++){let r=n/t.length;t[n]=Math.min(1,a*(.9-r*.6)*(.6+.4*Math.sin(e*(3+n*1.7)+n)))}return a}function ft(e,t,n){let r=e.getContext(`webgl2`,{alpha:!1,antialias:!1,depth:!1,stencil:!1,premultipliedAlpha:!0,preserveDrawingBuffer:!1,powerPreference:`low-power`});if(!r)throw Error(`WebGL 2 is not available`);let i=r,a=ee(),o=new ne(t.quality,t.debug.tier),s=new Ve(a,t.reduced),c=t.quality,l=t.reduced,u=t.visible,d=!1,f=!1,p=!1,m=t.debug.synth===!0,h,g=()=>{h=st(i,a),h.tier(C[o.tier])};g();let _=Math.max(1,t.width),v=Math.max(1,t.height),y=t.dpr>0?t.dpr:1;function b(){let t=Math.min(y,C[o.tier].dpr),n=Math.max(1,Math.round(_*t)),r=Math.max(1,Math.round(v*t));e.width!==n&&(e.width=n),e.height!==r&&(e.height=r),h.size(n,r,_,v),s.size(_,v)}let x=!0,S=null;fetch(t.stars).then(e=>{if(!e.ok)throw Error(`${t.stars}: ${e.status}`);return e.arrayBuffer()}).then(e=>{d||s.setSky(pe(e))}).catch(e=>n({type:`problem`,message:`the star catalogue didn't load: ${String(e?.message??e)}`})).finally(()=>{x=!1,V();let e=S;S=null,e&&_e(e)});let w=/* @__PURE__ */ new Float32Array(16),te=0;function ae(){!m||s.time-te<.03333333333333333||(te=s.time,s.state===`listening`?s.level(0,dt(s.time+3.1,w),w):s.state===`speaking`&&s.level(1,dt(s.time,w),w))}let T=i.getExtension(`EXT_disjoint_timer_query_webgl2`),E=[],D=null,O=null,oe=()=>{!T||E.length>=4||(D=i.createQuery(),D&&i.beginQuery(T.TIME_ELAPSED_EXT,D))},k=()=>{T&&D&&(i.endQuery(T.TIME_ELAPSED_EXT),E.push(D),D=null)},A=()=>{let e=E[0];if(T&&e&&i.getQueryParameter(e,i.QUERY_RESULT_AVAILABLE)){if(E.shift(),!i.getParameter(T.GPU_DISJOINT_EXT)){let t=i.getQueryParameter(e,i.QUERY_RESULT)/1e6;O=O===null?t:O+(t-O)*.1}i.deleteQuery(e)}},se=()=>{O=null},j=typeof requestAnimationFrame==`function`?e=>requestAnimationFrame(e):e=>setTimeout(()=>e(performance.now()),16),M=typeof cancelAnimationFrame==`function`?cancelAnimationFrame:clearTimeout,N=0,ce=0,P=0,le=new ie,ue=!1,F=0,I=0,de=0,L=0,fe=()=>u&&!d&&!f&&!l&&!p&&c!==`off`&&s.laid;function R(e){let t=e>.05?2:1;for(let n=0;n<t;n++)s.update(e/t),ae()}function z(e){if(N=0,!fe())return;if(N=j(z),!ce){ce=P=e;return}let t=e-ce;ce=e;let r=s.active();if(!le.due(t,r?60:24))return;let i=e-P;P=e;let a=Math.min(.1,i/1e3);A(),r&&ue&&o.frame(re(i,O))&&(W(),se()),ue=r;let c=performance.now();R(a),oe(),h.draw(s,!0),k(),L+=performance.now()-c,I++,de+=i,e-F>2e3&&(n({type:`stats`,fps:Math.round(I*1e3/(e-F)),ms:I?Math.round(de/I*10)/10:0,cpu:I?Math.round(L/I*100)/100:0,gpu:O===null?null:Math.round(O*100)/100,tier:o.tier}),F=e,I=0,de=0,L=0)}function B(){if(fe()){N||=(ce=0,le.reset(),ue=!1,o.reset(),j(z));return}N&&M(N),N=0,l&&u&&!d&&!f&&!p&&c!==`off`&&U()}let V=B,H=0;function U(){H||d||(H=j(()=>{H=0,!(!u||d||f||p||c===`off`)&&(s.settle(),h.draw(s,!1))}))}function W(){h.tier(C[o.tier]),b()}e.addEventListener(`webglcontextlost`,e=>{e.preventDefault(),f=!0,N&&M(N),N=0}),e.addEventListener(`webglcontextrestored`,()=>{f=!1,g(),b(),B()});async function G(e){let t=i.getExtension(`EXT_disjoint_timer_query_webgl2`);if(!t)return null;let n=[],r=!1;i.getParameter(t.GPU_DISJOINT_EXT),h.profile=e=>{r&&i.endQuery(t.TIME_ELAPSED_EXT),r=!1;let a=e?i.createQuery():null;a&&(i.beginQuery(t.TIME_ELAPSED_EXT,a),r=!0,n.push({pass:e,query:a}))};for(let t=0;t<e;t++)R(1/60),h.draw(s,!0);h.profile=null;let a={},o=n[n.length-1]?.query,c=performance.now()+5e3;for(;o&&!i.getQueryParameter(o,i.QUERY_RESULT_AVAILABLE)&&performance.now()<c;)await new Promise(e=>setTimeout(e,2));let l=!i.getParameter(t.GPU_DISJOINT_EXT);for(let{pass:e,query:t}of n)i.getQueryParameter(t,i.QUERY_RESULT_AVAILABLE)?a[e]=(a[e]??0)+i.getQueryParameter(t,i.QUERY_RESULT)/1e6:l=!1,i.deleteQuery(t);if(!l)return null;let u=0;for(let t of Object.keys(a))a[t]/=Math.max(1,e),u+=a[t];return{total:u,passes:a}}async function me(t){p=!0,N&&M(N),N=0;let r=o.tier,a=[],c=/* @__PURE__ */ new Uint8Array(4),l=()=>i.readPixels(0,0,1,1,i.RGBA,i.UNSIGNED_BYTE,c);for(let e=0;e<C.length&&!d;e++){o.tier=e,W();for(let e=0;e<8;e++)R(1/60),h.draw(s,!0);l();let n=performance.now();for(let e=0;e<t;e++)R(1/60),h.draw(s,!0),l();let r=(performance.now()-n)/Math.max(1,t);a.push({tier:e,ms:r,gpu:await G(t)})}d||(o.tier=r,W(),h.draw(s,!0),n({type:`bench`,size:[e.width,e.height],tiers:a}))}let he,ge=/* @__PURE__ */ new Float32Array(16);function _e(e){if(!d)switch(e.type){case`size`:_=Math.max(1,e.width),v=Math.max(1,e.height),y=e.dpr>0?e.dpr:1,b(),V();break;case`layout`:s.layout(e.horizon,e.talkX,e.talkY),V();break;case`state`:s.setState(e.state),V();break;case`level`:if(l)break;for(let t=0;t<16;t++)ge[t]=e.data[2+t]??0;s.level(e.data[0]===0?0:1,e.data[1],ge);break;case`pointer`:l||s.pointer(e.x,e.y);break;case`meteor`:!l&&c!==`off`&&s.meteor(e.hue,e.x,e.y);break;case`recall`:!l&&c!==`off`&&e.count>0&&s.recall(e.count);break;case`write`:if(c===`off`)break;s.write(e.text,e.shape,e.ms),l&&(clearTimeout(he),e.ms===0?s.writing&&s.writing.fadeAt<=s.time&&s.endWriting():he=setTimeout(()=>{s.endWriting(),U()},e.ms)),V();break;case`rest`:s.rest(e.on),V();break;case`quality`:e.quality!==c&&(c=e.quality,o.prefer(e.quality,t.debug.tier),W(),N&&M(N),N=0),B();break;case`visible`:u=e.on,B();break;case`motion`:l=e.reduced,s.reduced=l,B();break;case`advance`:{if(x){S=e;break}p=!0,N&&M(N),N=0;let t=Math.round(Math.max(0,e.seconds)*60);for(let e=0;e<t;e++)s.update(1/60),ae();h.draw(s,!0),n({type:`advanced`});break}case`bench`:x?S=e:me(e.frames);break;case`dispose`:d=!0,N&&M(N),H&&M(H),clearTimeout(he),h.dispose(),i.getExtension(`WEBGL_lose_context`)?.loseContext()}}return b(),n({type:`ready`,tier:o.tier}),B(),{handle:_e}}let pt;addEventListener(`message`,e=>{let t=e.data;if(t.type!==`init`){pt?.handle(t),t.type===`dispose`&&close();return}try{pt=ft(t.canvas,t.init,e=>postMessage(e))}catch(e){postMessage({type:`failed`,error:String(e?.message??e)})}});