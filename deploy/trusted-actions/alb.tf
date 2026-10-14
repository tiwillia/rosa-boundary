resource "aws_lb" "main" {
  name                       = var.app_name
  internal                   = true
  load_balancer_type         = "application"
  security_groups            = [aws_security_group.alb.id]
  subnets                    = [local.private_subnet_a, local.private_subnet_b]
  drop_invalid_header_fields = true
  tags                       = { Environment = var.environment }
}

resource "aws_lb_target_group" "app" {
  name        = var.app_name
  port        = 8080
  protocol    = "HTTP"
  vpc_id      = local.vpc_id
  target_type = "instance" # Phase 1: EC2. Phase 2: change to "ip" for Fargate awsvpc

  health_check {
    path                = "/health"
    protocol            = "HTTP"
    matcher             = "200"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  tags = { Environment = var.environment }
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.main.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = local.certificate_arn

  lifecycle {
    precondition {
      condition     = local.https_enabled
      error_message = "Private API requires an ACM certificate: set alb_certificate_arn or domain_name and route53_zone_id."
    }
  }

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.app.arn
  }
}
