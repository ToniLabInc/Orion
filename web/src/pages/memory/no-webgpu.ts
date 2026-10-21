// three-render-objects (under react-force-graph-3d) imports three's WebGPU renderer only to offer it as an option.
// The neural map draws with WebGL, so vite.config.ts points "three/webgpu" here: about 500 kB stay out of the build.
export class WebGPURenderer {}
