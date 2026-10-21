import{$ as e,G as t,J as n,K as r,U as i,W as a,at as o,et as s,it as c,nt as l,ot as u,q as d,rt as f,tt as p}from"./index-BRHcFGk-.js";var m=[{dpr:1,aurora:.5,sheets:1,faintest:5.5},{dpr:1.5,aurora:1,sheets:2,faintest:6.5},{dpr:2,aurora:1,sheets:2,faintest:6.5},{dpr:3,aurora:1.5,sheets:2,faintest:6.5}],h=m.length-1;function g(e){return e===`high`?h:e===`low`||e===`off`?1:2}var _=class{tier;min;max;avg=1e3/60;slow=0;easy=0;hold=10;sinceUp=1/0;constructor(e,t){this.tier=this.min=this.max=0,this.prefer(e,t)}prefer(e,t){t===void 0?e===`auto`?(this.min=0,this.max=h,this.tier=g(e)):this.tier=this.min=this.max=g(e):this.tier=this.min=this.max=Math.min(h,Math.max(0,Math.round(t))),this.hold=10,this.reset()}reset(){this.avg=1e3/60,this.slow=0,this.easy=0}frame(e){if(this.min===this.max||!(e>0))return 0;let t=Math.min(e,250)/1e3;return this.avg+=(Math.min(e,250)-this.avg)*.1,this.sinceUp+=t,this.avg>18?(this.slow+=t,this.easy=0):(this.slow=0,this.easy=this.avg<17.5?this.easy+t:0),this.slow>=2&&this.tier>this.min?(this.sinceUp<6&&(this.hold=Math.min(160,this.hold*2)),this.tier--,this.sinceUp=1/0,this.reset(),-1):this.easy>=this.hold&&this.tier<this.max?(this.tier++,this.sinceUp=0,this.reset(),1):0}};function ee(e,t){return t===null?e:Math.max(e,t*4)}var te=class{debt=0;vsync=1e3/60;reset(){this.debt=0}due(e,t){this.vsync=e<this.vsync?e:this.vsync+(e-this.vsync)*.02;let n=1e3/t;return this.debt+=e,this.debt+this.vsync*.5<n?!1:(this.debt=Math.min(this.debt-n,n),!0)}};function v(e,t,n){let r=e.createShader(t);if(!r)throw Error(`no shader`);if(e.shaderSource(r,n),e.compileShader(r),!e.getShaderParameter(r,e.COMPILE_STATUS)&&!e.isContextLost()){let t=e.getShaderInfoLog(r);throw e.deleteShader(r),Error(`shader: ${t}`)}return r}function y(e,t,n){let r=e.createProgram();if(!r)throw Error(`no program`);let i=v(e,e.VERTEX_SHADER,t),a=v(e,e.FRAGMENT_SHADER,n);if(e.attachShader(r,i),e.attachShader(r,a),e.linkProgram(r),e.deleteShader(i),e.deleteShader(a),!e.getProgramParameter(r,e.LINK_STATUS)&&!e.isContextLost())throw Error(`program: ${e.getProgramInfoLog(r)}`);let o={},s=e.getProgramParameter(r,e.ACTIVE_UNIFORMS);for(let t=0;t<s;t++){let n=e.getActiveUniform(r,t);n&&(o[n.name.replace(/\[0\]$/,``)]=e.getUniformLocation(r,n.name))}return{program:r,u:o}}function b(e,t,n,r,i,a=null,o=e.CLAMP_TO_EDGE){let s=e.createTexture();if(!s)throw Error(`no texture`);return e.bindTexture(e.TEXTURE_2D,s),e.texImage2D(e.TEXTURE_2D,0,r.internal,t,n,0,r.format,r.type,a),e.texParameteri(e.TEXTURE_2D,e.TEXTURE_MIN_FILTER,i),e.texParameteri(e.TEXTURE_2D,e.TEXTURE_MAG_FILTER,i===e.NEAREST?e.NEAREST:e.LINEAR),e.texParameteri(e.TEXTURE_2D,e.TEXTURE_WRAP_S,o),e.texParameteri(e.TEXTURE_2D,e.TEXTURE_WRAP_T,o),i===e.LINEAR_MIPMAP_LINEAR&&e.generateMipmap(e.TEXTURE_2D),s}function ne(e,t,n,r,i,a=1){let o=e.createFramebuffer();if(!o)throw Error(`no framebuffer`);e.bindFramebuffer(e.FRAMEBUFFER,o);let s=[],c=[];for(let o=0;o<a;o++){let a=b(e,t,n,r,i);e.framebufferTexture2D(e.FRAMEBUFFER,e.COLOR_ATTACHMENT0+o,e.TEXTURE_2D,a,0),s.push(a),c.push(e.COLOR_ATTACHMENT0+o)}a>1&&e.drawBuffers(c);let l=e.checkFramebufferStatus(e.FRAMEBUFFER);if(e.bindFramebuffer(e.FRAMEBUFFER,null),l!==e.FRAMEBUFFER_COMPLETE&&!e.isContextLost()){e.deleteFramebuffer(o);for(let t of s)e.deleteTexture(t);throw Error(`framebuffer incomplete (${l.toString(16)})`)}return{fbo:o,tex:s,w:t,h:n}}var x=[[[4,1]],[[16,1]],[[64,1]],[[32,.65],[64,.35]]],S=e=>e*e*e*(e*(e*6-15)+10);function re(e,t){let n=new Float32Array(e*e);for(let e=0;e<n.length;e++)n[e]=t();let r=new Float32Array(65536),i=e/256;for(let t=0;t<256;t++){let a=t*i,o=Math.floor(a),s=S(a-o),c=o%e*e,l=(o+1)%e*e;for(let a=0;a<256;a++){let o=a*i,u=Math.floor(o),d=S(o-u),f=(u+1)%e,p=n[c+u%e]+(n[c+f]-n[c+u%e])*d,m=n[l+u%e]+(n[l+f]-n[l+u%e])*d;r[t*256+a]=p+(m-p)*s}}return r}function ie(e=20){let t=d(e),n=new Uint8Array(262144);return x.forEach((e,r)=>{let i=new Float32Array(65536);for(let[n,r]of e){let e=re(n,t);for(let t=0;t<i.length;t++)i[t]+=e[t]*r}let a=1/0,o=-1/0;for(let e of i)a=Math.min(a,e),o=Math.max(o,e);for(let e=0;e<i.length;e++)n[e*4+r]=Math.round((i[e]-a)/(o-a)*255)}),n}var C=4096,w=[1024,372,71,54];function T(e,t){let n=new Float32Array(e);for(let r=0;r<e;r++)n[r]=t();let r=new Float32Array(C),i=e/C;for(let t=0;t<C;t++){let a=t*i,o=Math.floor(a),s=S(a-o);r[t]=n[o%e]+(n[(o+1)%e]-n[o%e])*s}return r}function ae(e=41){let t=d(e),n=new Uint8Array(C*4);return w.forEach((e,r)=>{let i=T(e,t),a=1/0,o=-1/0;for(let e of i)a=Math.min(a,e),o=Math.max(o,e);for(let e=0;e<C;e++)n[e*4+r]=Math.round((i[e]-a)/(o-a)*255)}),n}var E=`#version 300 es
precision highp float;
precision highp int;
precision highp sampler2D;
const float PI=3.14159265;
const float TAU=6.28318531;
`,D=`vec3 encode(vec3 c){
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
`,O=`uniform vec2 uCss;
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
`,k=`vec2 cssPos(){return vec2(gl_FragCoord.x/uPx.x,uCss.y - gl_FragCoord.y/uPx.y);}
`,A=`uniform sampler2D uString;
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
`,j=`uniform mat3 uViewInv;
uniform vec4 uFrame;
uniform vec2 uShift;
vec2 place(vec3 q){
float k=2.0/(1.0 + q.z);
return vec2(uFrame.y + q.x*k*uFrame.x,uFrame.z - q.y*k*uFrame.x)+ uShift;
}
`,oe=`uniform sampler2D uNoise;
uniform sampler2D uMilkyWay;
uniform vec4 uMW;
uniform vec4 uGlow;
uniform vec3 uColB;
uniform vec3 uUpB;
uniform vec3 uSky;
uniform vec3 uNebula;
uniform vec3 uDust;
uniform vec4 uM42;
${j}
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
`,se=`${E}${O}
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
`,ce=`${E}${D}${O}${k}${oe}
in float vA;
out vec4 outColor;
void main(){
outColor=vec4(encode(skyAt(cssPos(),vA))+ dither(gl_FragCoord.xy),1.0);
}
`,le=`${E}${D}${O}${k}${A}${oe}
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
`,M=`${E}${D}${O}${A}
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
`,N=`${E}${D}${O}${j}
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
`,ue=`${E}${D}${O}${k}
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
`,P=`uniform float uMargin;
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
`,de=`${E}${O}${P}
layout(location=0)in vec4 aSeg;
layout(location=1)in vec4 aPen;
uniform vec2 uOrigin;
void main(){quad(aSeg.xy + uOrigin,aSeg.zw + uOrigin,aPen);}
`,fe=`${E}${O}${j}${P}
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
`,pe=`${E}${D}
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
`,F=`${E}${O}
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
`,me=`${E}${D}
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
`,I=7,he=8,L=8,ge=8,R=9,z=74,B=1e6,_e=60,V=256,H=4,ve=20;function ye(n,r){let o=y(n,se,ce),d=y(n,se,le),m=y(n,se,M),h=y(n,N,ue),g=y(n,fe,pe),_=y(n,de,pe),ee=y(n,F,me),te=r.ground.map(e=>e<=.0031308?12.92*e:1.055*e**(1/2.4)-.055),v=()=>{let e=n.createBuffer();if(!e)throw Error(`no buffer`);return e},x=()=>{let e=n.createVertexArray();if(!e)throw Error(`no vertex array`);return e},S=(e,t,r)=>{n.bindBuffer(n.ARRAY_BUFFER,e);for(let[e,i,a]of r)n.enableVertexAttribArray(e),n.vertexAttribPointer(e,i,n.FLOAT,!1,t*4,a*4),n.vertexAttribDivisor(e,1)},re=v(),w=x();n.bindVertexArray(w),S(re,I,[[0,3,0],[1,1,3],[2,3,4]]),n.bindVertexArray(null);let T=v(),E=x();n.bindVertexArray(E),S(T,he,[[0,3,0],[1,1,3],[2,3,4],[3,1,7]]),n.bindVertexArray(null),n.bindBuffer(n.ARRAY_BUFFER,T),n.bufferData(n.ARRAY_BUFFER,8*he*4,n.DYNAMIC_DRAW);let D=v(),O=x();n.bindVertexArray(O),S(D,L,[[0,3,0],[1,3,3],[2,2,6]]),n.bindVertexArray(null);let k=v(),A=x();n.bindVertexArray(A),S(k,ge,[[0,4,0],[1,4,4]]),n.bindVertexArray(null);let j=v(),oe=x();n.bindVertexArray(oe),S(j,R,[[0,4,0],[1,2,4],[2,3,6]]),n.bindVertexArray(null),n.bindBuffer(n.ARRAY_BUFFER,j),n.bufferData(n.ARRAY_BUFFER,8*R*4,n.DYNAMIC_DRAW),n.vertexAttrib1f(3,0),n.pixelStorei(n.UNPACK_ALIGNMENT,1);let P={internal:n.RGBA8,format:n.RGBA,type:n.UNSIGNED_BYTE},H={internal:n.R8,format:n.RED,type:n.UNSIGNED_BYTE},ye=ie(),Ce=b(n,256,256,P,n.LINEAR,ye,n.REPEAT),we=b(n,C,1,P,n.LINEAR,ae(),n.REPEAT),Te=b(n,1,1,H,n.LINEAR,new Uint8Array(1)),U=null,Ee=``,De=b(n,512,1,{internal:n.RGBA32F,format:n.RGBA,type:n.FLOAT},n.NEAREST,null),Oe=-1,W=1,G=1,K=1,q=1,J=null,ke=0,Ae=0,je=-1,Me=-1,Ne=0,Y=null,Pe=-1,Fe=-1,Ie=-1/0,Le=new Float32Array(8*he),Re=new Float32Array(8*R),ze=e=>{if(!e.sky)return;let t=`${e.width}|${e.height}|${e.frame.scale}|${e.frame.x}|${e.frame.y}`;t!==Ee&&(Ee=t,U=u(e.width,e.height,e.frame,e.view,ye,256),Ve++,n.deleteTexture(Te),Te=b(n,U.w,U.h,H,n.LINEAR,U.data))},Be={target:null,sig:null,used:-1/0},X={target:null,sig:null,used:-1/0},Ve=0,He=e=>{e.target&&(n.deleteFramebuffer(e.target.fbo),n.deleteTexture(e.target.tex)),e.target=null,e.sig=null},Ue=(e,t)=>{let n=e.limb,r=e.colours;return[W,G,K,q,n.cx,n.cy,n.r,n.horizon,e.frame.scale,e.frame.x,e.frame.y,e.skyOpen,+!!e.sky,Ve,...e.m42,e.glow,...xe(e),...r.b,...r.upB,t]},We=(e,t)=>{let n=e.colours;return[...Ue(e,t),e.limb.length,e.talkArc,be(e.patch,V),e.auroraTop,...n.a,...n.upA]},Ge=(e,t,r)=>{if(!e.target){let t=ne(n,W,G,P,n.NEAREST);e.target={fbo:t.fbo,tex:t.tex[0],w:W,h:G}}let i=Float32Array.from(t),a=e.sig;return(!a||a.length!==i.length||i.some((e,t)=>e!==a[t]))&&(n.bindFramebuffer(n.FRAMEBUFFER,e.target.fbo),n.viewport(0,0,W,G),r(),n.bindFramebuffer(n.FRAMEBUFFER,null),e.sig=i),e.target},Ke=(e,t)=>{n.bindFramebuffer(n.READ_FRAMEBUFFER,e.fbo),n.bindFramebuffer(n.DRAW_FRAMEBUFFER,t),n.blitFramebuffer(0,0,W,G,0,0,W,G,n.COLOR_BUFFER_BIT,n.NEAREST),n.bindFramebuffer(n.FRAMEBUFFER,null)},qe=()=>{Y&&=(n.deleteFramebuffer(Y.fbo),n.deleteTexture(Y.tex),null)},Je=e=>{let t=Math.min(J.aurora,W/K,G/q),r=Math.max(1,Math.round(K*t)),i=Math.max(1,Math.round(q*t)),{horizon:a,r:o}=e.limb;if(Y&&Y.w===r&&Y.h===i&&Pe===a&&Fe===o)return Y;qe();let s=ne(n,r,i,{internal:n.SRGB8_ALPHA8,format:n.RGBA,type:n.UNSIGNED_BYTE},n.LINEAR);return Y={fbo:s.fbo,tex:s.tex[0],w:r,h:i},Pe=a,Fe=o,n.bindFramebuffer(n.FRAMEBUFFER,s.fbo),n.clearColor(0,0,0,1),n.clear(n.COLOR_BUFFER_BIT),Y},Ye=e=>{let t=e.sky;if(!t||Ae===t.count)return;let r=new Float32Array(t.count*I);for(let n=0;n<t.count;n++)r.set(t.dirs.subarray(n*3,n*3+3),n*I),r[n*I+3]=t.vmag[n],r.set(e.starColors.subarray(n*3,n*3+3),n*I+4);n.bindBuffer(n.ARRAY_BUFFER,re),n.bufferData(n.ARRAY_BUFFER,r,n.STATIC_DRAW),Ae=t.count,Xe(e),je=-1},Xe=e=>{let t=e.sky;if(!t||!J)return;let n=0,r=t.count;for(;n<r;){let e=n+r>>1;t.vmag[e]<=J.faintest?n=e+1:r=e}ke=n},Ze=e=>{let t=e.sky;if(!t||je===e.figureRev)return;let r=t.lines.length/2,i=new Float32Array(r*L);for(let n=0;n<r;n++){let r=t.lines[n*2],a=t.lines[n*2+1];i.set(t.dirs.subarray(r*3,r*3+3),n*L),i.set(t.dirs.subarray(a*3,a*3+3),n*L+3),i[n*L+6]=e.figureTimes[n*2],i[n*L+7]=e.figureTimes[n*2+1]}n.bindBuffer(n.ARRAY_BUFFER,D),n.bufferData(n.ARRAY_BUFFER,i,n.STATIC_DRAW),je=e.figureRev},Qe=e=>{let t=e.writing;if(!t||Me===t.rev)return;let r=new Float32Array(t.segments.length*ge);t.segments.forEach((e,t)=>{r.set([e.x0,e.y0,e.x1,e.y1,e.t0,e.t1,e.ink0,e.ink1],t*ge)}),n.bindBuffer(n.ARRAY_BUFFER,k),n.bufferData(n.ARRAY_BUFFER,r,n.DYNAMIC_DRAW),Ne=t.segments.length,Me=t.rev},$e=(e,t,r,i)=>{let{u:a}=e,o=t.limb;n.uniform2f(a.uCss,t.width,t.height),n.uniform2f(a.uPx,r,i),n.uniform4f(a.uLimb,o.cx,o.cy,o.r,o.horizon),n.uniform2f(a.uSpan,o.start,1/o.length)},et=(e,t,r)=>{let{u:i}=e;n.uniformMatrix3fv(i.uViewInv,!1,t.view),n.uniform4f(i.uFrame,t.frame.scale,t.frame.x,t.frame.y,r),n.uniform2f(i.uShift,t.shift[0],t.shift[1])},Z=(e,t)=>{n.activeTexture(n.TEXTURE0+e),n.bindTexture(n.TEXTURE_2D,t)},Q=(e,t)=>n.uniform3f(e,t[0],t[1],t[2]),$=e=>n.useProgram(e.program),tt=(e,t,r,i)=>{let a=t.limb,o=Math.asin(Math.min(1,(K/2+40)/(a.r+Math.min(r,0))));n.uniform4f(e.u.uBand,r,i,o,128),n.drawArrays(n.TRIANGLE_STRIP,0,258)},nt=e=>{let t=J,i=Je(e);n.bindFramebuffer(n.FRAMEBUFFER,i.fbo),n.viewport(0,0,i.w,i.h),$(m);let{u:a}=m;$e(m,e,i.w/K,i.h/q),Z(0,De),Z(1,Ce),Z(4,we),n.uniform1i(a.uString,0),n.uniform1i(a.uNoise,1),n.uniform1i(a.uRays,4),n.uniform4f(a.uLive,0,e.bumpCount,0,0),n.uniform4f(a.uAur,e.aurora,e.auroraTop,t.sheets,e.auroraDrift),n.uniform4fv(a.uBump,e.bumpsForShader()),Q(a.uAuLow,r.auroraLow),Q(a.uAuHigh,r.auroraHigh),n.uniform1f(a.uTime,e.flicker),tt(m,e,-30,e.auroraTop),n.bindFramebuffer(n.FRAMEBUFFER,null)},rt=(t,i)=>{let{u:a}=t;$e(t,i,W/K,G/q),et(t,i,i.sky?Se(i.skyOpen):0);let[o,s]=xe(i);n.uniform2f(a.uShift,o,s),Z(1,Ce),Z(3,Te),n.uniform1i(a.uNoise,1),n.uniform1i(a.uMilkyWay,3),U&&n.uniform4f(a.uMW,32,1/(K+64),1/(q+64),e),n.uniform4f(a.uGlow,i.glow,i.voice,i.mic,i.sweep);let c=i.colours;Q(a.uColB,c.b),Q(a.uUpB,c.upB),Q(a.uSky,r.sky),Q(a.uNebula,r.nebula),Q(a.uDust,r.dust),n.uniform4f(a.uM42,i.m42[0],i.m42[1],i.m42[2],i.m42[3])},it=(e,t)=>{n.clearColor(te[0],te[1],te[2],1),n.clear(n.COLOR_BUFFER_BIT);let r=e.limb.horizon+8;r>t&&($(o),rt(o,e),tt(o,e,t,r))},at=(e,t,i)=>{$(d);let{u:a}=d;rt(d,e),Z(0,De),n.uniform1i(a.uString,0),n.uniform4f(a.uLive,+!!e.stringLive,e.bumpCount,+(e.mic>.002),+(e.sweep>-1e3)),t&&Y&&(Z(2,Y.tex),n.uniform1i(a.uAurora,2)),n.uniform2f(a.uAurSeen,+!!t,e.auroraTop),n.uniform1f(a.uPatch,be(e.patch,V));let o=e.colours;Q(a.uColA,o.a),Q(a.uUpA,o.upA),n.uniform3f(a.uWipe,e.talkArc,Math.min(e.wipeFront,B),160),n.uniform4fv(a.uBump,e.bumpsForShader()),Q(a.uGround,r.ground),tt(d,e,-80,Math.min(i,e.limb.horizon+8))},ot=(e,t,r)=>{let i=t?Math.max(e.auroraTop+8,220):220;if(!r){it(e,i),at(e,t,i);return}Be.used=e.time;let a=Ge(Be,Ue(e,i),()=>it(e,i));if(t||!e.backgroundStill()){Ke(a,null),at(e,t,i);return}X.used=e.time;let o=Ge(X,We(e,i),()=>{Ke(a,X.target.fbo),n.bindFramebuffer(n.FRAMEBUFFER,X.target.fbo),at(e,!1,i)});Ke(o,null)},st=(e,t)=>{let r=Se(e.skyOpen);if(!e.sky||r<=0)return;n.enable(n.BLEND),n.blendEquation(n.FUNC_ADD),n.blendFunc(n.ONE,n.ONE_MINUS_SRC_COLOR),$(h);let{u:i}=h;$e(h,e,W/K,G/q),et(h,e,r),n.uniform4f(i.uStar,l,p*r,f,c),n.uniform1f(i.uCut,s),n.uniform1f(i.uTime,e.flicker),n.uniform1f(i.uTwinkle,+!!t),n.bindVertexArray(w),n.drawArraysInstanced(n.TRIANGLE_STRIP,0,4,ke);let a=e.recallsForShader(Le);a>0&&(n.bindBuffer(n.ARRAY_BUFFER,T),n.bufferSubData(n.ARRAY_BUFFER,0,Le,0,a*he),n.bindVertexArray(E),n.drawArraysInstanced(n.TRIANGLE_STRIP,0,4,a)),n.bindVertexArray(null)},ct=(e,t,r,i,a)=>{let{u:o}=e;n.uniform1f(o.uT,t),Q(o.uColour,r),n.uniform4f(o.uStroke,i[0],i[1],i[2],i[3]),n.uniform3f(o.uInk,a[0],a[1],a[2])},lt=e=>{let t=e.sky;if(!t||e.figureAlpha<.003||e.skyOpen<=0)return;Ze(e),$(g);let{u:i}=g;$e(g,e,W/K,G/q),et(g,e,1),n.uniform1f(i.uMargin,6),n.uniform1f(i.uGap,7),ct(g,e.figureClock,r.figure,[.5,0,1,.38*e.figureAlpha],[0,1,.9]),n.bindVertexArray(O),n.drawArraysInstanced(n.TRIANGLE_STRIP,0,4,t.lines.length/2),n.bindVertexArray(null)},ut=e=>{let t=e.writing,r=e.writeGain();if(!t||r<=0)return;Qe(e),$(_);let{u:i}=_;$e(_,e,W/K,G/q),n.uniform1f(i.uMargin,16),n.uniform2f(i.uOrigin,t.origin[0],t.origin[1]),ct(_,e.time-t.t0,t.colour,[.8,.08,3.2,r],[1.2,.35,2.2]),n.bindVertexArray(A),n.drawArraysInstanced(n.TRIANGLE_STRIP,0,4,Ne),n.bindVertexArray(null)},dt=e=>{let r=e.meteorsForShader(Re);if(r===0)return;n.enable(n.BLEND),n.blendEquation(n.FUNC_ADD),n.blendFunc(n.ONE,n.ONE_MINUS_SRC_COLOR),$(ee);let{u:i}=ee;$e(ee,e,W/K,G/q),n.uniform2f(i.uTrail,t,a),n.bindBuffer(n.ARRAY_BUFFER,j),n.bufferSubData(n.ARRAY_BUFFER,0,Re,0,r*R),n.bindVertexArray(oe),n.drawArraysInstanced(n.TRIANGLE_STRIP,0,z,r),n.bindVertexArray(null)},ft=null,pt=e=>ft?.(e);return{get profile(){return ft},set profile(e){ft=e},size(e,t,n,r){W=e,G=t,K=n,q=r,qe(),He(Be),He(X)},tier(e){let t=J?.aurora!==e.aurora;J=e,t&&qe()},draw(e,t){if(n.viewport(0,0,W,G),n.disable(n.BLEND),!e.laid||!J){n.clearColor(0,0,0,1),n.clear(n.COLOR_BUFFER_BIT);return}Ye(e),ze(e),Xe(e),e.stringRev!==Oe&&(Z(0,De),n.texSubImage2D(n.TEXTURE_2D,0,0,0,512,1,n.RGBA,n.FLOAT,e.stringData),Oe=e.stringRev);let r=e.aurora>i;r?(Ie=e.time,pt(`aurora`),nt(e)):Y&&e.time-Ie>ve&&qe();for(let t of[Be,X])t.target&&e.time-t.used>_e&&He(t);n.viewport(0,0,W,G),pt(`background`),ot(e,r,t),pt(`stars`),st(e,t),n.disable(n.BLEND),(e.figureAlpha>=.003||e.writing)&&(pt(`strokes`),n.enable(n.BLEND),n.blendEquation(n.MAX),n.blendFunc(n.ONE,n.ONE),lt(e),ut(e),n.blendEquation(n.FUNC_ADD)),pt(`meteors`),dt(e),n.disable(n.BLEND),pt(``)},dispose(){qe(),He(Be),He(X);for(let e of[o,d,m,h,g,_,ee])n.deleteProgram(e.program);for(let e of[re,T,D,k,j])n.deleteBuffer(e);for(let e of[w,E,O,A,oe])n.deleteVertexArray(e);n.deleteTexture(Ce),n.deleteTexture(we),n.deleteTexture(Te),n.deleteTexture(De)}}}var be=(e,t)=>Math.floor(e*t)/t,xe=e=>[Math.round(e.shift[0]*H)/H,Math.round(e.shift[1]*H)/H],Se=e=>{let t=Math.min(1,Math.max(0,e));return t*t*(3-2*t)};function Ce(e,t){let n=Math.max(0,Math.sin(e*Math.PI*2*4.3))**1.5,r=.55+.45*Math.sin(e*Math.PI*2*.37),i=Math.sin(e*Math.PI*2*.21)>-.7?1:.08,a=Math.min(1,(.15+.85*n)*r*i);for(let n=0;n<t.length;n++){let r=n/t.length;t[n]=Math.min(1,a*(.9-r*.6)*(.6+.4*Math.sin(e*(3+n*1.7)+n)))}return a}var we=.05,Te=1/30;function U(e,t,i){let a=e.getContext(`webgl2`,{alpha:!1,antialias:!1,depth:!1,stencil:!1,premultipliedAlpha:!0,preserveDrawingBuffer:!1,powerPreference:`low-power`});if(!a)throw Error(`WebGL 2 is not available`);let s=a,c=n(),l=new _(t.quality,t.debug.tier),u=new r(c,t.reduced),d=t.quality,f=t.reduced,p=t.visible,h=!1,g=!1,v=!1,y=t.debug.synth===!0,b,ne=()=>{b=ye(s,c),b.tier(m[l.tier])};ne();let x=Math.max(1,t.width),S=Math.max(1,t.height),re=t.dpr>0?t.dpr:1;function ie(){let t=Math.min(re,m[l.tier].dpr),n=Math.max(1,Math.round(x*t)),r=Math.max(1,Math.round(S*t));e.width!==n&&(e.width=n),e.height!==r&&(e.height=r),b.size(n,r,x,S),u.size(x,S)}let C=!0,w=null;fetch(t.stars).then(e=>{if(!e.ok)throw Error(`${t.stars}: ${e.status}`);return e.arrayBuffer()}).then(e=>{h||u.setSky(o(e))}).catch(e=>i({type:`problem`,message:`the star catalogue didn't load: ${String(e?.message??e)}`})).finally(()=>{C=!1,z();let e=w;w=null,e&&Se(e)});let T=new Float32Array(16),ae=0;function E(){!y||u.time-ae<Te||(ae=u.time,u.state===`listening`?u.level(0,Ce(u.time+3.1,T),T):u.state===`speaking`&&u.level(1,Ce(u.time,T),T))}let D=s.getExtension(`EXT_disjoint_timer_query_webgl2`),O=[],k=null,A=null,j=()=>{!D||O.length>=4||(k=s.createQuery(),k&&s.beginQuery(D.TIME_ELAPSED_EXT,k))},oe=()=>{D&&k&&(s.endQuery(D.TIME_ELAPSED_EXT),O.push(k),k=null)},se=()=>{let e=O[0];if(D&&e&&s.getQueryParameter(e,s.QUERY_RESULT_AVAILABLE)){if(O.shift(),!s.getParameter(D.GPU_DISJOINT_EXT)){let t=s.getQueryParameter(e,s.QUERY_RESULT)/1e6;A=A===null?t:A+(t-A)*.1}s.deleteQuery(e)}},ce=()=>{A=null},le=typeof requestAnimationFrame==`function`?e=>requestAnimationFrame(e):e=>setTimeout(()=>e(performance.now()),16),M=typeof cancelAnimationFrame==`function`?cancelAnimationFrame:clearTimeout,N=0,ue=0,P=0,de=new te,fe=!1,pe=0,F=0,me=0,I=0,he=()=>p&&!h&&!g&&!f&&!v&&d!==`off`&&u.laid;function L(e){let t=e>we?2:1;for(let n=0;n<t;n++)u.update(e/t),E()}function ge(e){if(N=0,!he())return;if(N=le(ge),!ue){ue=P=e;return}let t=e-ue;ue=e;let n=u.active();if(!de.due(t,n?60:24))return;let r=e-P;P=e;let a=Math.min(.1,r/1e3);se(),n&&fe&&l.frame(ee(r,A))&&(V(),ce()),fe=n;let o=performance.now();L(a),j(),b.draw(u,!0),oe(),I+=performance.now()-o,F++,me+=r,e-pe>2e3&&(i({type:`stats`,fps:Math.round(F*1e3/(e-pe)),ms:F?Math.round(me/F*10)/10:0,cpu:F?Math.round(I/F*100)/100:0,gpu:A===null?null:Math.round(A*100)/100,tier:l.tier}),pe=e,F=0,me=0,I=0)}function R(){if(he()){N||=(ue=0,de.reset(),fe=!1,l.reset(),le(ge));return}N&&M(N),N=0,f&&p&&!h&&!g&&!v&&d!==`off`&&_e()}let z=R,B=0;function _e(){B||h||(B=le(()=>{B=0,!(!p||h||g||v||d===`off`)&&(u.settle(),b.draw(u,!1))}))}function V(){b.tier(m[l.tier]),ie()}e.addEventListener(`webglcontextlost`,e=>{e.preventDefault(),g=!0,N&&M(N),N=0}),e.addEventListener(`webglcontextrestored`,()=>{g=!1,ne(),ie(),R()});async function H(e){let t=s.getExtension(`EXT_disjoint_timer_query_webgl2`);if(!t)return null;let n=[],r=!1;s.getParameter(t.GPU_DISJOINT_EXT),b.profile=e=>{r&&s.endQuery(t.TIME_ELAPSED_EXT),r=!1;let i=e?s.createQuery():null;i&&(s.beginQuery(t.TIME_ELAPSED_EXT,i),r=!0,n.push({pass:e,query:i}))};for(let t=0;t<e;t++)L(1/60),b.draw(u,!0);b.profile=null;let i={},a=n[n.length-1]?.query,o=performance.now()+5e3;for(;a&&!s.getQueryParameter(a,s.QUERY_RESULT_AVAILABLE)&&performance.now()<o;)await new Promise(e=>setTimeout(e,2));let c=!s.getParameter(t.GPU_DISJOINT_EXT);for(let{pass:e,query:t}of n)s.getQueryParameter(t,s.QUERY_RESULT_AVAILABLE)?i[e]=(i[e]??0)+s.getQueryParameter(t,s.QUERY_RESULT)/1e6:c=!1,s.deleteQuery(t);if(!c)return null;let l=0;for(let t of Object.keys(i))i[t]/=Math.max(1,e),l+=i[t];return{total:l,passes:i}}async function ve(t){v=!0,N&&M(N),N=0;let n=l.tier,r=[],a=new Uint8Array(4),o=()=>s.readPixels(0,0,1,1,s.RGBA,s.UNSIGNED_BYTE,a);for(let e=0;e<m.length&&!h;e++){l.tier=e,V();for(let e=0;e<8;e++)L(1/60),b.draw(u,!0);o();let n=performance.now();for(let e=0;e<t;e++)L(1/60),b.draw(u,!0),o();let i=(performance.now()-n)/Math.max(1,t);r.push({tier:e,ms:i,gpu:await H(t)})}h||(l.tier=n,V(),b.draw(u,!0),i({type:`bench`,size:[e.width,e.height],tiers:r}))}let be,xe=new Float32Array(16);function Se(e){if(!h)switch(e.type){case`size`:x=Math.max(1,e.width),S=Math.max(1,e.height),re=e.dpr>0?e.dpr:1,ie(),z();break;case`layout`:u.layout(e.horizon,e.talkX,e.talkY),z();break;case`state`:u.setState(e.state),z();break;case`level`:if(f)break;for(let t=0;t<16;t++)xe[t]=e.data[2+t]??0;u.level(e.data[0]===0?0:1,e.data[1],xe);break;case`pointer`:f||u.pointer(e.x,e.y);break;case`meteor`:!f&&d!==`off`&&u.meteor(e.hue,e.x,e.y);break;case`recall`:!f&&d!==`off`&&e.count>0&&u.recall(e.count);break;case`write`:if(d===`off`)break;u.write(e.text,e.shape,e.ms),f&&(clearTimeout(be),e.ms===0?u.writing&&u.writing.fadeAt<=u.time&&u.endWriting():be=setTimeout(()=>{u.endWriting(),_e()},e.ms)),z();break;case`rest`:u.rest(e.on),z();break;case`quality`:e.quality!==d&&(d=e.quality,l.prefer(e.quality,t.debug.tier),V(),N&&M(N),N=0),R();break;case`visible`:p=e.on,R();break;case`motion`:f=e.reduced,u.reduced=f,R();break;case`advance`:{if(C){w=e;break}v=!0,N&&M(N),N=0;let t=Math.round(Math.max(0,e.seconds)*60);for(let e=0;e<t;e++)u.update(1/60),E();b.draw(u,!0),i({type:`advanced`});break}case`bench`:C?w=e:ve(e.frames);break;case`dispose`:h=!0,N&&M(N),B&&M(B),clearTimeout(be),b.dispose(),s.getExtension(`WEBGL_lose_context`)?.loseContext()}}return ie(),i({type:`ready`,tier:l.tier}),R(),{handle:Se}}export{U as startEngine};