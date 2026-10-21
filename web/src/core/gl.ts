// The little Horizon needs from WebGL 2: programs (their uniforms looked up once), textures and the framebuffers that
// draw into them. No three.js: the scene is a handful of draws.

export interface Program {
  program: WebGLProgram
  u: Record<string, WebGLUniformLocation | null>
}

function shader(gl: WebGL2RenderingContext, type: number, source: string): WebGLShader {
  const s = gl.createShader(type)
  if (!s) throw new Error("no shader")
  gl.shaderSource(s, source)
  gl.compileShader(s)
  if (!gl.getShaderParameter(s, gl.COMPILE_STATUS) && !gl.isContextLost()) {
    const log = gl.getShaderInfoLog(s)
    gl.deleteShader(s)
    throw new Error(`shader: ${log}`)
  }
  return s
}

/**
 * Compile and link a program, and look up every uniform it declares.
 * @param gl the context
 * @param vertex the vertex shader's source
 * @param fragment the fragment shader's source
 * @returns the program and its uniforms by name (an array's by its name without `[0]`)
 */
export function program(gl: WebGL2RenderingContext, vertex: string, fragment: string): Program {
  const p = gl.createProgram()
  if (!p) throw new Error("no program")
  const vs = shader(gl, gl.VERTEX_SHADER, vertex)
  const fs = shader(gl, gl.FRAGMENT_SHADER, fragment)
  gl.attachShader(p, vs)
  gl.attachShader(p, fs)
  gl.linkProgram(p)
  gl.deleteShader(vs)
  gl.deleteShader(fs)
  if (!gl.getProgramParameter(p, gl.LINK_STATUS) && !gl.isContextLost())
    throw new Error(`program: ${gl.getProgramInfoLog(p)}`)
  const u: Program["u"] = {}
  const count = gl.getProgramParameter(p, gl.ACTIVE_UNIFORMS) as number
  for (let i = 0; i < count; i++) {
    const info = gl.getActiveUniform(p, i)
    if (info) u[info.name.replace(/\[0\]$/, "")] = gl.getUniformLocation(p, info.name)
  }
  return { program: p, u }
}

/** A texture's storage: its internal format, and the format and type its data comes in. */
export interface Format {
  internal: number
  format: number
  type: number
}

/**
 * A 2D texture, clamped at its edges.
 * @param gl the context
 * @param w its width
 * @param h its height
 * @param f its format
 * @param filter NEAREST or LINEAR (LINEAR_MIPMAP_LINEAR: mipmapped, its levels allocated)
 * @param data what it starts with (null: undefined)
 * @param wrap CLAMP_TO_EDGE or REPEAT, both ways
 * @returns the texture
 */
export function texture(
  gl: WebGL2RenderingContext,
  w: number,
  h: number,
  f: Format,
  filter: number,
  data: ArrayBufferView | null = null,
  wrap: number = gl.CLAMP_TO_EDGE,
): WebGLTexture {
  const t = gl.createTexture()
  if (!t) throw new Error("no texture")
  gl.bindTexture(gl.TEXTURE_2D, t)
  gl.texImage2D(gl.TEXTURE_2D, 0, f.internal, w, h, 0, f.format, f.type, data)
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, filter)
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, filter === gl.NEAREST ? gl.NEAREST : gl.LINEAR)
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, wrap)
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, wrap)
  if (filter === gl.LINEAR_MIPMAP_LINEAR) gl.generateMipmap(gl.TEXTURE_2D)
  return t
}

/**
 * A framebuffer over one or more new textures of the same size (more than one: the shader writes them all at once).
 * @param gl the context
 * @param w the width
 * @param h the height
 * @param f the textures' format
 * @param filter how they are sampled
 * @param count how many textures
 * @returns the framebuffer and its textures
 */
export function framebuffer(
  gl: WebGL2RenderingContext,
  w: number,
  h: number,
  f: Format,
  filter: number,
  count = 1,
): { fbo: WebGLFramebuffer; tex: WebGLTexture[]; w: number; h: number } {
  const fbo = gl.createFramebuffer()
  if (!fbo) throw new Error("no framebuffer")
  gl.bindFramebuffer(gl.FRAMEBUFFER, fbo)
  const tex: WebGLTexture[] = []
  const buffers: number[] = []
  for (let i = 0; i < count; i++) {
    const t = texture(gl, w, h, f, filter)
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0 + i, gl.TEXTURE_2D, t, 0)
    tex.push(t)
    buffers.push(gl.COLOR_ATTACHMENT0 + i)
  }
  if (count > 1) gl.drawBuffers(buffers)
  const status = gl.checkFramebufferStatus(gl.FRAMEBUFFER)
  gl.bindFramebuffer(gl.FRAMEBUFFER, null)
  if (status !== gl.FRAMEBUFFER_COMPLETE && !gl.isContextLost()) {
    gl.deleteFramebuffer(fbo)
    for (const t of tex) gl.deleteTexture(t)
    throw new Error(`framebuffer incomplete (${status.toString(16)})`)
  }
  return { fbo, tex, w, h }
}
