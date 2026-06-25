#pragma once

// =============================================================================
//  ukf_soh.h  —  Team Imperium | Caterpillar Tech Challenge
//
//  On-device joint SOC + SOH estimator using an Unscented Kalman Filter.
//
//  This is the C++ port of the PC-side Python filter in  soc_ukf.py  — both
//  implement the IDENTICAL model, parameters and sigma-point tuning, so the
//  ESP32 and the dashboard produce the same SOC/SOH estimate.
//
//  Header-only, no dynamic allocation, fixed 3x3 / length-3 math. Uses 32-bit
//  float to match the ESP32 hardware FPU. Safe to call once per frame at 10 Hz.
//
//  MODEL  (1-RC Thevenin ECM + joint capacity state)
//  -------------------------------------------------
//    State  x = [ SOC , Vrc , SOH ]        (SOC,SOH in 0..~1, Vrc in volts)
//
//    Process (current I in A, discharge negative, dt in s):
//      cap_As   = Q_NOM_AH * SOH * 3600
//      SOC[k+1] = SOC[k] + I*dt / cap_As
//      Vrc[k+1] = Vrc[k]*exp(-dt/tau) + I*R1*(1 - exp(-dt/tau)),  tau = R1*C1
//      SOH[k+1] = SOH[k]                    (slow random walk)
//
//    Measurement (terminal voltage):
//      V = OCV(SOC) + (I - I_NOM)*R0 + Vrc
//
//  Usage:
//    UkfSoh ukf;
//    ukf.begin(/*soc0=*/1.0f);
//    ukf.step(voltage_avg, current_A, dt_s);
//    float soc = ukf.socPct();   float soh = ukf.sohPct();
// =============================================================================

#include <math.h>

// ── Cell / model parameters (must match soc_ukf.py verbatim) ──────────────────
#define UKF_Q_NOM_AH   2.9f      // nominal cell capacity                 [Ah]
#define UKF_R0         0.030f    // ohmic series resistance               [Ohm]
#define UKF_R1         0.015f    // RC-pair resistance                    [Ohm]
#define UKF_C1         2000.0f   // RC-pair capacitance                   [F]
#define UKF_I_NOM      (-2.9f)   // nominal current the OCV curve fits at [A]

#define UKF_Q_SOC      1.0e-7f   // SOC process variance / step
#define UKF_Q_VRC      1.0e-5f   // Vrc process variance / step
#define UKF_Q_SOH      1.0e-9f   // SOH process variance / step (slow)
#define UKF_R_VOLT     1.0e-4f   // voltage measurement variance (10 mV sigma)

#define UKF_P0_SOC     1.0e-2f
#define UKF_P0_VRC     1.0e-3f
#define UKF_P0_SOH     2.0e-2f

// Van der Merwe scaled sigma points: alpha=1, kappa=0 => lambda=0, beta=2.
// All weights are O(1) — numerically safe in float32.
#define UKF_N          3
#define UKF_NSIG       (2 * UKF_N + 1)   // = 7


/** NMC terminal-voltage-vs-SOC curve fitted to the dataset at I_NOM.
 *  (Degree-5 polynomial, identical coefficients to soc_ukf.py's ocv().) */
static inline float ukf_ocv(float soc) {
    float s = soc < 0.0f ? 0.0f : (soc > 1.0f ? 1.0f : soc);
    return ((((( -0.411719f * s + 0.063607f) * s + 1.274983f) * s
              - 1.732312f) * s + 2.062802f) * s + 2.777598f);
}


class UkfSoh {
public:
    void begin(float soc0 = 1.0f, float soh0 = 1.0f) {
        _x[0] = _clamp(soc0, 0.0f, 1.0f);
        _x[1] = 0.0f;
        _x[2] = soh0;

        for (int i = 0; i < UKF_N; i++)
            for (int j = 0; j < UKF_N; j++)
                _Pcov[i][j] = 0.0f;
        _Pcov[0][0] = UKF_P0_SOC;
        _Pcov[1][1] = UKF_P0_VRC;
        _Pcov[2][2] = UKF_P0_SOH;

        // lambda = 0  →  gamma = sqrt(n)
        _gamma = sqrtf((float)UKF_N);
        const float nl = (float)UKF_N;     // n + lambda = 3
        _Wm[0] = 0.0f;                     // lambda / (n+lambda)
        _Wc[0] = 0.0f + (1.0f - 1.0f + 2.0f);   // + (1 - alpha^2 + beta) = 2
        for (int i = 1; i < UKF_NSIG; i++) {
            _Wm[i] = 1.0f / (2.0f * nl);
            _Wc[i] = _Wm[i];
        }
        _ready = true;
    }

    bool ready() const { return _ready; }

    /** Advance the filter one step with a terminal-voltage measurement. */
    void step(float voltage, float current, float dt) {
        if (!_ready) begin(1.0f);
        if (dt <= 0.0f) dt = 0.1f;

        // --- generate sigma points: x and x +/- gamma*col(chol(3*P)) ---
        float A[UKF_N][UKF_N];
        for (int i = 0; i < UKF_N; i++)
            for (int j = 0; j < UKF_N; j++)
                A[i][j] = (float)UKF_N * _Pcov[i][j];   // (n+lambda)=3
        float L[UKF_N][UKF_N];
        _chol3(A, L);

        float sig[UKF_NSIG][UKF_N];
        for (int j = 0; j < UKF_N; j++) sig[0][j] = _x[j];
        for (int c = 0; c < UKF_N; c++) {
            for (int r = 0; r < UKF_N; r++) {
                sig[1 + c][r]          = _x[r] + L[r][c];
                sig[1 + UKF_N + c][r]  = _x[r] - L[r][c];
            }
        }

        // --- predict: propagate through process model ---
        float prop[UKF_NSIG][UKF_N];
        for (int i = 0; i < UKF_NSIG; i++)
            _f(sig[i], current, dt, prop[i]);

        float xp[UKF_N] = {0, 0, 0};
        for (int i = 0; i < UKF_NSIG; i++)
            for (int j = 0; j < UKF_N; j++)
                xp[j] += _Wm[i] * prop[i][j];

        float Pp[UKF_N][UKF_N];
        Pp[0][0] = UKF_Q_SOC; Pp[1][1] = UKF_Q_VRC; Pp[2][2] = UKF_Q_SOH;
        Pp[0][1] = Pp[0][2] = Pp[1][0] = Pp[1][2] = Pp[2][0] = Pp[2][1] = 0.0f;
        for (int i = 0; i < UKF_NSIG; i++) {
            float d[UKF_N];
            for (int j = 0; j < UKF_N; j++) d[j] = prop[i][j] - xp[j];
            for (int a = 0; a < UKF_N; a++)
                for (int b = 0; b < UKF_N; b++)
                    Pp[a][b] += _Wc[i] * d[a] * d[b];
        }

        // --- update: terminal voltage measurement ---
        float z[UKF_NSIG];
        for (int i = 0; i < UKF_NSIG; i++) z[i] = _h(prop[i], current);
        float zp = 0.0f;
        for (int i = 0; i < UKF_NSIG; i++) zp += _Wm[i] * z[i];

        float Pzz = UKF_R_VOLT;
        float Pxz[UKF_N] = {0, 0, 0};
        for (int i = 0; i < UKF_NSIG; i++) {
            float dz = z[i] - zp;
            Pzz += _Wc[i] * dz * dz;
            for (int j = 0; j < UKF_N; j++)
                Pxz[j] += _Wc[i] * (prop[i][j] - xp[j]) * dz;
        }

        float K[UKF_N];
        for (int j = 0; j < UKF_N; j++) K[j] = Pxz[j] / Pzz;

        float innov = voltage - zp;
        for (int j = 0; j < UKF_N; j++) _x[j] = xp[j] + K[j] * innov;
        for (int a = 0; a < UKF_N; a++)
            for (int b = 0; b < UKF_N; b++)
                _Pcov[a][b] = Pp[a][b] - K[a] * K[b] * Pzz;

        // keep states physical + covariance symmetric / SPD
        _x[0] = _clamp(_x[0], 0.0f, 1.0f);
        _x[2] = _clamp(_x[2], 0.5f, 1.2f);
        for (int a = 0; a < UKF_N; a++) {
            for (int b = a + 1; b < UKF_N; b++) {
                float m = 0.5f * (_Pcov[a][b] + _Pcov[b][a]);
                _Pcov[a][b] = _Pcov[b][a] = m;
            }
            _Pcov[a][a] += 1.0e-12f;
        }
    }

    float soc()    const { return _x[0]; }          // 0..1
    float soh()    const { return _x[2]; }           // ~1.0
    float vrc()    const { return _x[1]; }           // volts
    float socPct() const { return _x[0] * 100.0f; }
    float sohPct() const { return _x[2] * 100.0f; }

private:
    float _x[UKF_N];
    float _Pcov[UKF_N][UKF_N];
    float _Wm[UKF_NSIG];
    float _Wc[UKF_NSIG];
    float _gamma = 1.0f;
    bool  _ready = false;

    static inline float _clamp(float v, float lo, float hi) {
        return v < lo ? lo : (v > hi ? hi : v);
    }

    // Process model: out = f(x, current, dt)
    static void _f(const float* x, float current, float dt, float* out) {
        float soh = _clampS(x[2], 0.5f, 1.2f);
        float cap_as = UKF_Q_NOM_AH * soh * 3600.0f;
        float soc_n = x[0] + (current * dt) / cap_as;
        float a = expf(-dt / (UKF_R1 * UKF_C1));
        out[0] = soc_n < 0.0f ? 0.0f : (soc_n > 1.0f ? 1.0f : soc_n);
        out[1] = x[1] * a + current * UKF_R1 * (1.0f - a);
        out[2] = soh;
    }

    // Measurement model: terminal voltage from state
    static float _h(const float* x, float current) {
        return ukf_ocv(x[0]) + (current - UKF_I_NOM) * UKF_R0 + x[1];
    }

    static inline float _clampS(float v, float lo, float hi) {
        return v < lo ? lo : (v > hi ? hi : v);
    }

    // Lower-triangular Cholesky of a 3x3 SPD matrix A -> L (L*L^T = A).
    // Diagonal arguments are floored to a small positive to stay defined.
    static void _chol3(const float A[3][3], float L[3][3]) {
        for (int i = 0; i < 3; i++)
            for (int j = 0; j < 3; j++) L[i][j] = 0.0f;

        float d0 = A[0][0];
        L[0][0] = sqrtf(d0 > 1e-12f ? d0 : 1e-12f);
        L[1][0] = A[1][0] / L[0][0];
        L[2][0] = A[2][0] / L[0][0];

        float d1 = A[1][1] - L[1][0] * L[1][0];
        L[1][1] = sqrtf(d1 > 1e-12f ? d1 : 1e-12f);
        L[2][1] = (A[2][1] - L[2][0] * L[1][0]) / L[1][1];

        float d2 = A[2][2] - L[2][0] * L[2][0] - L[2][1] * L[2][1];
        L[2][2] = sqrtf(d2 > 1e-12f ? d2 : 1e-12f);
    }
};
