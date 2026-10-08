resource "aws_ecs_cluster" "this" {
  name = "${var.project}-cluster"
}

resource "aws_cloudwatch_log_group" "server" {
  name              = "/ecs/${var.project}/server"
  retention_in_days = 14
}

resource "aws_cloudwatch_log_group" "web" {
  name              = "/ecs/${var.project}/web"
  retention_in_days = 14
}

resource "aws_service_discovery_private_dns_namespace" "this" {
  name        = var.namespace_name
  description = "${var.project} internal service discovery"
  vpc         = var.vpc_id
}

resource "aws_service_discovery_service" "server" {
  name = "server"

  dns_config {
    namespace_id = aws_service_discovery_private_dns_namespace.this.id
    dns_records {
      ttl  = 10
      type = "A"
    }
    routing_policy = "MULTIVALUE"
  }

}

resource "aws_lb" "this" {
  name               = "${var.project}-alb"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [var.alb_sg_id]
  subnets            = var.public_subnet_ids
  enable_http2       = var.enable_http2
}

resource "aws_lb_target_group" "web" {
  name        = "${var.project}-web-tg"
  port        = 3000
  protocol    = "HTTP"
  vpc_id      = var.vpc_id
  target_type = "ip"

  health_check {
    path                = "/"
    matcher             = "200-399"
    interval            = 30
    healthy_threshold   = 2
    unhealthy_threshold = 5
  }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.this.arn
  port              = 80
  protocol          = "HTTP"

  # With a certificate, :80 only bounces to :443; without one it serves the web
  # as before. A redirect action must not carry target_group_arn at all, hence
  # two mutually exclusive dynamic blocks rather than one with conditionals.
  dynamic "default_action" {
    for_each = var.enable_https ? [] : [1]
    content {
      type             = "forward"
      target_group_arn = aws_lb_target_group.web.arn
    }
  }

  dynamic "default_action" {
    for_each = var.enable_https ? [1] : []
    content {
      type = "redirect"
      redirect {
        port        = "443"
        protocol    = "HTTPS"
        status_code = "HTTP_301"
      }
    }
  }
}

# Web over TLS. Browser features that need a secure context (crypto.subtle for
# PKCE, clipboard, randomUUID) and IdPs that refuse http redirect URIs both make
# this the real entry point once a certificate is attached.
resource "aws_lb_listener" "https" {
  count             = var.enable_https ? 1 : 0
  load_balancer_arn = aws_lb.this.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.web.arn
  }
}

resource "aws_lb_target_group" "sandbox" {
  name        = "${var.project}-sandbox-tg"
  port        = 3000
  protocol    = "HTTP"
  vpc_id      = var.vpc_id
  target_type = "ip"

  health_check {
    path                = "/"
    matcher             = "200-399"
    interval            = 30
    healthy_threshold   = 2
    unhealthy_threshold = 5
  }
}

resource "aws_lb_listener" "sandbox" {
  # MCP Apps 샌드박스 전용 출처. 호스트(:80)와 포트가 달라 브라우저 출처가 분리된다.
  load_balancer_arn = aws_lb.this.arn
  port              = 8081
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.sandbox.arn
  }
}

# Sandbox over TLS. An https page cannot embed an http:8081 origin (mixed
# content); :8443 keeps the "different port = different origin" rule the MCP Apps
# spec relies on and is covered by the same certificate.
resource "aws_lb_listener" "sandbox_https" {
  count             = var.enable_https ? 1 : 0
  load_balancer_arn = aws_lb.this.arn
  port              = 8443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.sandbox.arn
  }
}

locals {
  server_env_list = [for k, v in var.server_env : { name = k, value = v }]
  web_env_list    = [for k, v in var.web_env : { name = k, value = v }]
}

resource "aws_ecs_task_definition" "server" {
  family                   = "${var.project}-server"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.server_cpu
  memory                   = var.server_memory
  execution_role_arn       = var.execution_role_arn
  task_role_arn            = var.task_role_arn

  container_definitions = jsonencode([{
    name      = "server"
    image     = var.server_image
    essential = true
    portMappings = [{
      containerPort = 8000
      protocol      = "tcp"
    }]
    environment = local.server_env_list
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.server.name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "server"
      }
    }
  }])
}

resource "aws_ecs_task_definition" "web" {
  family                   = "${var.project}-web"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.web_cpu
  memory                   = var.web_memory
  execution_role_arn       = var.execution_role_arn
  task_role_arn            = var.task_role_arn

  container_definitions = jsonencode([{
    name      = "web"
    image     = var.web_image
    essential = true
    portMappings = [{
      containerPort = 3000
      protocol      = "tcp"
    }]
    environment = local.web_env_list
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.web.name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "web"
      }
    }
  }])
}

# The services follow whichever revision of their family is newest: the one this
# apply registers, or one registered out of band (a hotfix `register-task-definition`
# + `update-service`). The data source reads the family's latest revision at plan
# time; `max()` against the resource's own revision makes an image change in tfvars
# roll out (resource revision is the newer) while a later manual revision is not
# rolled back by the next apply (data revision is the newer). `desired_count` is a
# plain variable because park.sh drives it with `-var desired_count=0`.
#
# `depends_on` makes the data source read after the resource is (re)registered;
# without it the data read can race the new revision on the same apply.
data "aws_ecs_task_definition" "server" {
  task_definition = aws_ecs_task_definition.server.family
  depends_on      = [aws_ecs_task_definition.server]
}

data "aws_ecs_task_definition" "web" {
  task_definition = aws_ecs_task_definition.web.family
  depends_on      = [aws_ecs_task_definition.web]
}

locals {
  # Full ARN, not family:revision — the provider stores the ARN, and a
  # differently-formatted equal value would plan as a change on every run.
  server_task_definition = "${aws_ecs_task_definition.server.arn_without_revision}:${max(
    aws_ecs_task_definition.server.revision,
    data.aws_ecs_task_definition.server.revision,
  )}"
  web_task_definition = "${aws_ecs_task_definition.web.arn_without_revision}:${max(
    aws_ecs_task_definition.web.revision,
    data.aws_ecs_task_definition.web.revision,
  )}"
}

resource "aws_ecs_service" "server" {
  name            = "server"
  cluster         = aws_ecs_cluster.this.id
  task_definition = local.server_task_definition
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [var.server_sg_id]
    assign_public_ip = false
  }

  service_registries {
    registry_arn = aws_service_discovery_service.server.arn
  }
}

resource "aws_ecs_service" "web" {
  name            = "web"
  cluster         = aws_ecs_cluster.this.id
  task_definition = local.web_task_definition
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [var.web_sg_id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.web.arn
    container_name   = "web"
    container_port   = 3000
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.sandbox.arn
    container_name   = "web"
    container_port   = 3000
  }

  depends_on = [aws_lb_listener.http, aws_lb_listener.sandbox]
}
