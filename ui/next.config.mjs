/** @type {import('next').NextConfig} */
const nextConfig = {
  // Static export — deployed to S3 + CloudFront
  output: 'export',
  trailingSlash: true,

  transpilePackages: ['aws-amplify'],
  reactStrictMode: true,

  env: {
    NEXT_PUBLIC_API_URL: process.env.NEXT_PUBLIC_API_URL || '',
    NEXT_PUBLIC_POLL_INTERVAL: process.env.NEXT_PUBLIC_POLL_INTERVAL || '30000',
    NEXT_PUBLIC_AUTH_ENABLED: process.env.NEXT_PUBLIC_AUTH_ENABLED || 'false',
    NEXT_PUBLIC_COGNITO_USER_POOL_ID: process.env.NEXT_PUBLIC_COGNITO_USER_POOL_ID || '',
    NEXT_PUBLIC_COGNITO_CLIENT_ID: process.env.NEXT_PUBLIC_COGNITO_CLIENT_ID || '',
    NEXT_PUBLIC_COGNITO_REGION: process.env.NEXT_PUBLIC_COGNITO_REGION || 'us-east-1',
  },

  // NOTE: response headers are NOT configured here.
  //
  // With `output: 'export'` there is no Next.js server at runtime, so a
  // `headers()` block is silently ignored (Next emits a build warning to that
  // effect). The two concerns it used to cover are handled at the edge instead:
  //   - Security headers (X-Content-Type-Options, X-Frame-Options, HSTS,
  //     Referrer-Policy) — CloudFront ResponseHeadersPolicy attached to the
  //     distribution's default cache behavior (sam/template.yaml, ADR #96).
  //   - Cache-Control (immutable for /_next/static, no-cache for HTML/config.js)
  //     — set on the S3 objects at upload time (sam/deploy-ui.sh, ui/deploy.sh).
};

export default nextConfig;
