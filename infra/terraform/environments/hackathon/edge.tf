# The public HTTPS entry point: a CloudFront distribution whose only origin is
# the INTERNAL load balancer, reached through a CloudFront VPC origin.
#
# Why CloudFront and not the ALB directly:
#   * HTTPS without a domain. The Vercel frontend is served over HTTPS, so a
#     browser refuses to call a plain-HTTP API (mixed content). An ACM
#     certificate needs a domain this project does not own; *.cloudfront.net
#     comes with a valid certificate for free.
#   * The ALB stays internal: no internet listener, no public IPv4 charges.
#   * Cost: origin fetches from a VPC origin are free and the always-free tier
#     (1 TB out, 10 M requests a month) dwarfs demo traffic.
#
# Why SSE survives it: CloudFront's origin response timeout bounds the gap
# BETWEEN packets, not the length of a response, and both SSE endpoints emit a
# heartbeat at least every 15 s. Caching is disabled, so nothing is buffered.
# Verify with the smoke test in the deploy workflow; it is the one property
# here no amount of reading can prove.

# CloudFront's origin-facing address ranges, maintained by AWS.
data "aws_ec2_managed_prefix_list" "cloudfront_origin_facing" {
  name = "com.amazonaws.global.cloudfront.origin-facing"
}

# The internal ALB's ONLY ingress rule. Traffic from a VPC origin arrives from
# CloudFront's origin-facing ranges (documented option 1 for VPC origins), and
# since the ALB is internal and a VPC origin can only be created by this
# account, nothing else can use the rule to reach it.
resource "aws_vpc_security_group_ingress_rule" "alb_from_cloudfront" {
  security_group_id = module.network.alb_security_group_id
  description       = "HTTP from CloudFront VPC origin only"
  prefix_list_id    = data.aws_ec2_managed_prefix_list.cloudfront_origin_facing.id
  from_port         = 80
  to_port           = 80
  ip_protocol       = "tcp"
}

resource "aws_cloudfront_vpc_origin" "alb" {
  vpc_origin_endpoint_config {
    name                   = "${local.name_prefix}-alb"
    arn                    = aws_lb.api.arn
    http_port              = 80
    https_port             = 443
    origin_protocol_policy = "http-only"

    origin_ssl_protocols {
      items    = ["TLSv1.2"]
      quantity = 1
    }
  }

  tags = merge(local.tags, { component = "edge" })
}

# AWS-managed policies, by their published ids.
#   CachingDisabled: every request goes to the origin; an API response is never
#   served from cache to a different caller.
#   AllViewer: forwards every viewer header - including Authorization, which
#   CloudFront strips by default and which an origin request policy cannot name
#   individually - plus Last-Event-ID, Origin and the ingest token header.
locals {
  cache_policy_caching_disabled = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"
  origin_request_all_viewer     = "216adef6-5c7f-47e4-b989-5492eafa07d3"
}

resource "aws_cloudfront_distribution" "api" {
  enabled         = true
  comment         = "Aegis hackathon API (backend only; the frontend is on Vercel)"
  price_class     = var.cloudfront_price_class
  is_ipv6_enabled = true
  http_version    = "http2and3"

  origin {
    origin_id   = "alb"
    domain_name = aws_lb.api.dns_name

    vpc_origin_config {
      vpc_origin_id            = aws_cloudfront_vpc_origin.alb.id
      origin_read_timeout      = var.origin_read_timeout
      origin_keepalive_timeout = 60
    }
  }

  default_cache_behavior {
    target_origin_id       = "alb"
    viewer_protocol_policy = "https-only"
    allowed_methods        = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods         = ["GET", "HEAD"]
    # Compression would make the edge buffer text/event-stream to compress it.
    compress                 = false
    cache_policy_id          = local.cache_policy_caching_disabled
    origin_request_policy_id = local.origin_request_all_viewer
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  # *.cloudfront.net certificate. Its minimum TLS version is fixed by AWS; a
  # custom domain with an ACM certificate (us-east-1) would allow pinning
  # TLSv1.2_2021 - see the architecture doc.
  viewer_certificate {
    cloudfront_default_certificate = true
  }

  tags = merge(local.tags, { component = "edge" })
}
