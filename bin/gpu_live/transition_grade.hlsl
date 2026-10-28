// OKK STUDIO live GPU compositor. Independently implemented grading math
// matches realtime_grade.py; transition equations follow FFmpeg vf_xfade.c.
// Blend occurs after each source's camera -> local -> global grade chain.
// The host renders both libmpv textures on one D3D11 device, then presents once.

Texture2D<float4> SourceA : register(t0);
Texture2D<float4> SourceB : register(t1);
// Reserved interfaces for later per-source LUT support; currently unused.
Texture3D<float4> SourceLutA : register(t2);
Texture3D<float4> SourceLutB : register(t3);
SamplerState VideoSampler : register(s0);

cbuffer RenderState : register(b0) {
    // kind id, elapsed progress 0..1, display width, display height.
    float4 Transition;
    // A camera/local/global, then B camera/local/global. 204 floats per layer.
    // Packed float4 keeps this entire constant buffer at 4912 bytes.
    float4 GradeData[306];
};

struct VertexOutput {
    float4 position : SV_Position;
    float2 uv : TEXCOORD0;
};

VertexOutput VSMain(uint vertexId : SV_VertexID) {
    VertexOutput output;
    output.uv = float2((vertexId << 1) & 2, vertexId & 2);
    output.position = float4(output.uv.x * 2.0 - 1.0, 1.0 - output.uv.y * 2.0, 0.0, 1.0);
    return output;
}

float parameter(int actor, int layer, int offset) {
    int index = actor * 612 + layer * 204 + offset;
    return GradeData[index >> 2][index & 3];
}

float channel_max(float3 value) { return max(value.r, max(value.g, value.b)); }
float channel_min(float3 value) { return min(value.r, min(value.g, value.b)); }
float positive_mod(float value, float divisor) { return value - divisor * floor(value / divisor); }
float3 positive_mod3(float3 value, float divisor) { return value - divisor * floor(value / divisor); }

float3 primary_balance(float3 color, float temperature, float tint) {
    float original_sum = channel_max(color) + channel_min(color);
    float weight = saturate((original_sum - 0.333) * 4.0 + 0.5)
                 * saturate((1.0 - original_sum - 0.333) * 4.0 + 0.5) * 0.7;
    color = saturate(color + weight * float3(temperature * 0.25 - tint * 0.1,
                                             tint * 0.2, -temperature * 0.25 - tint * 0.1));
    float high = channel_max(color), low = channel_min(color), delta = high - low;
    float hue = 0.0;
    if (delta > 0.0000001) {
        if (high == color.r) hue = (color.g - color.b) / delta;
        else if (high == color.g) hue = 2.0 + (color.b - color.r) / delta;
        else hue = 4.0 + (color.r - color.g) / delta;
        hue = positive_mod(hue, 6.0);
    }
    float lightness = original_sum * 0.5;
    float saturation = (high >= 1.0 || low <= 0.0) ? 0.0
        : delta / max(0.0000001, 1.0 - abs(2.0 * lightness - 1.0));
    float3 k = positive_mod3(float3(0.0, 8.0, 4.0) + hue * 2.0, 12.0);
    return saturate(lightness - saturation * min(lightness, 1.0 - lightness)
        * max(min(min(k - 3.0, 9.0 - k), 1.0), -1.0));
}

float3 primary_shadow_curve(float3 color, float shadows, float highlights) {
    float y1 = 0.25 + shadows * 0.2, y2 = 0.75 + highlights * 0.2;
    float r1 = 6.0 * (2.0 * y2 - 6.0 * y1);
    float r2 = 6.0 * (4.0 - 6.0 * y2 + 2.0 * y1);
    float m1 = (1.5 * r1 - 0.5 * r2) * 0.5;
    float m2 = (1.5 * r2 - 0.5 * r1) * 0.5;
    float3 output = color;
    [unroll] for (int channel = 0; channel < 3; channel++) {
        float a, b, width, left, right, ml, mr;
        if (color[channel] < 0.25) { width = 0.25; b = color[channel] / width; left = 0.0; right = y1; ml = 0.0; mr = m1; }
        else if (color[channel] < 0.75) { width = 0.5; b = (color[channel] - 0.25) / width; left = y1; right = y2; ml = m1; mr = m2; }
        else { width = 0.25; b = (color[channel] - 0.75) / width; left = y2; right = 1.0; ml = m2; mr = 0.0; }
        a = 1.0 - b;
        output[channel] = saturate(a * left + b * right
            + ((a * a * a - a) * ml + (b * b * b - b) * mr) * width * width / 6.0);
    }
    return output;
}

float custom_curve(float value, int actor, int layer, int curve) {
    value = saturate(value);
    int base = 32 + curve * 43;
    float count = parameter(actor, layer, base);
    [loop] for (int interval = 0; interval < 7; interval++) {
        int offset = base + 1 + interval * 6;
        float right = parameter(actor, layer, offset + 1);
        if (float(interval) < count && (value <= right || float(interval + 1) >= count)) {
            float t = value - parameter(actor, layer, offset);
            float a = parameter(actor, layer, offset + 2);
            float b = parameter(actor, layer, offset + 3);
            float c = parameter(actor, layer, offset + 4);
            float d = parameter(actor, layer, offset + 5);
            return saturate(a + t * (b + t * (c + t * d)));
        }
    }
    return value;
}

float3 portrait_grade(float3 color, float lighten, float saturation, float deyellow) {
    if (lighten <= 0.0 && saturation <= 0.0 && deyellow <= 0.0) return color;
    float y = dot(color, float3(0.2126, 0.7152, 0.0722));
    float cb = 0.5 + dot(color, float3(-0.168736, -0.331264, 0.5));
    float cr = 0.5 + dot(color, float3(0.5, -0.418688, -0.081312));
    float distance = pow((cb - 0.42) / 0.13, 2.0) + pow((cr - 0.60) / 0.12, 2.0);
    float chroma = channel_max(color) - channel_min(color);
    float mask = (1.0 - smoothstep(0.55, 1.25, distance))
        * smoothstep(0.015, 0.06, color.r - color.b) * smoothstep(0.005, 0.05, color.r - color.g)
        * smoothstep(0.025, 0.09, chroma) * (1.0 - smoothstep(0.45, 0.65, chroma))
        * smoothstep(0.035, 0.16, y) * (1.0 - smoothstep(0.82, 0.97, y));
    float correction = min(max(0.0, (color.r + color.g) * 0.5 - color.b - 0.02), max(0.0, color.g - color.b) * 1.15);
    float amount = deyellow * mask * smoothstep(0.002, 0.025, color.g - color.b);
    color.b += correction * amount;
    color.r -= max(0.0, color.r - color.g) * 0.06 * amount;
    y = dot(color, float3(0.2126, 0.7152, 0.0722));
    color = lerp(color, y.xxx, saturation * 0.18 * mask);
    return saturate(lerp(color, float3(1.0, 1.0, 1.0), lighten * 0.22 * mask * (1.0 - y)));
}

float3 grade_layer(float3 color, int actor, int layer) {
    [branch] if (parameter(actor, layer, 0) >= 0.5) {
    color = saturate(color * float3(parameter(actor, layer, 28), parameter(actor, layer, 29), parameter(actor, layer, 30)));
    color = pow(saturate((color * exp2(parameter(actor, layer, 1)) - 0.5) * parameter(actor, layer, 2) + 0.5),
                1.0 / parameter(actor, layer, 6));
    float3 lift = float3(parameter(actor, layer, 9), parameter(actor, layer, 10), parameter(actor, layer, 11)) + parameter(actor, layer, 12);
    float3 gamma = max(0.05, 1.0 + float3(parameter(actor, layer, 13), parameter(actor, layer, 14), parameter(actor, layer, 15)) + parameter(actor, layer, 16));
    float3 gain = max(0.0, 1.0 + float3(parameter(actor, layer, 17), parameter(actor, layer, 18), parameter(actor, layer, 19)) + parameter(actor, layer, 20));
    float3 offset = float3(parameter(actor, layer, 21), parameter(actor, layer, 22), parameter(actor, layer, 23)) + parameter(actor, layer, 24);
    color = saturate(pow(saturate((color * (1.0 - lift) + lift) * gain), 1.0 / gamma) + offset);
    float saturation = parameter(actor, layer, 3);
    float factor = saturation > 2.0 ? sqrt(saturation) : saturation;
    [unroll] for (int stage = 0; stage < 2; stage++) {
        if (stage == 1 && saturation <= 2.0) break;
        float luminance = dot(color, float3(0.2126, 0.7152, 0.0722));
        color = saturate(lerp(luminance.xxx, color, factor));
    }
    float temperature = parameter(actor, layer, 4), tint = parameter(actor, layer, 5);
    if (temperature != 0.0 || tint != 0.0) color = primary_balance(color, temperature, tint);
    float shadows = parameter(actor, layer, 7), highlights = parameter(actor, layer, 8);
    if (shadows != 0.0 || highlights != 0.0) color = primary_shadow_curve(color, shadows, highlights);
    if (parameter(actor, layer, 31) > 0.5) {
        color = float3(custom_curve(color.r, actor, layer, 0), custom_curve(color.g, actor, layer, 1), custom_curve(color.b, actor, layer, 2));
        color = float3(custom_curve(color.r, actor, layer, 3), custom_curve(color.g, actor, layer, 3), custom_curve(color.b, actor, layer, 3));
    }
    color = portrait_grade(color, parameter(actor, layer, 25), parameter(actor, layer, 26), parameter(actor, layer, 27));
    }
    return color;
}

float3 grade_source(float3 color, int actor) {
    [unroll] for (int layer = 0; layer < 3; layer++) color = grade_layer(color, actor, layer);
    return color;
}

float3 read_a(float2 uv) { return grade_source(SourceA.SampleLevel(VideoSampler, uv, 0.0).rgb, 0); }
float3 read_b(float2 uv) { return grade_source(SourceB.SampleLevel(VideoSampler, uv, 0.0).rgb, 1); }

float4 PSMain(VertexOutput input) : SV_Target {
    int kind = int(Transition.x);
    float time = saturate(Transition.y), progress = 1.0 - time;
    float2 dimensions = max(float2(1.0, 1.0), Transition.zw);
    int width = int(dimensions.x), height = int(dimensions.y);
    int2 pixel = int2(floor(input.position.xy));
    float2 sample_uv = input.uv;
    bool sliding = (kind == 4 || kind == 5) && time > 0.0 && time < 1.0;
    bool slide_uses_b = false;
    if (sliding) {
        int shift = kind == 4 ? int(-progress * width) : int(progress * width);
        int source_x = pixel.x + shift;
        int wrapped = (source_x % width + width) % width;
        sample_uv = float2((wrapped + 0.5) / dimensions.x, input.uv.y);
        slide_uses_b = source_x >= 0 && source_x < width;
    }
    float3 a = 0.0, b = 0.0, output = 0.0;
    [branch] if (kind < 0 || time < 1.0) a = read_a(sample_uv);
    [branch] if (kind >= 0 && time > 0.0) b = read_b(sample_uv);
    if (kind < 0 || time <= 0.0) return float4(a, 1.0);
    if (time >= 1.0) return float4(b, 1.0);
    if (sliding) return float4(slide_uses_b ? b : a, 1.0);
    if (kind == 1) {
        // Fixed per-pixel stochastic dissolve, not a cross-fade substitute.
        precise float phase = float(pixel.x) * 12.9898 + float(pixel.y) * 78.233;
        float random_value = frac(sin(phase) * 43758.545);
        float threshold = random_value * 2.0 + progress * 2.0 - 1.5;
        output = threshold >= 0.5 ? a : b;
    } else if (kind == 2) {
        output = pixel.x > int(width * progress) ? b : a;
    } else if (kind == 3) {
        output = pixel.x > int(width * (1.0 - progress)) ? a : b;
    } else if (kind == 6) {
        float2 centre = float2(width / 2, height / 2);
        float radius = max(1.0, length(centre));
        float amount = smoothstep(0.0, 1.0, length(float2(pixel) - centre) / radius + (progress - 0.5) * 3.0);
        output = a * amount + b * (1.0 - amount);
    } else if (kind == 7 || kind == 8) {
        float3 background = kind == 8 ? float3(1.0, 1.0, 1.0) : float3(0.0, 0.0, 0.0);
        float outgoing = smoothstep(0.8, 1.0, progress);
        float incoming = smoothstep(0.2, 1.0, progress);
        output = (a * outgoing + background * (1.0 - outgoing)) * progress
               + (background * incoming + b * (1.0 - incoming)) * (1.0 - progress);
    } else {
        output = lerp(a, b, time);
    }
    return float4(saturate(output), 1.0);
}
