/*
Copyright (c) 2026 Red Hat, Inc.

Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in
compliance with the License. You may obtain a copy of the License at

  http://www.apache.org/licenses/LICENSE-2.0
*/

// Package service runs the metering loops: one fold, one round, and one relay per tenant,
// discovered by listing event topics. The loops meet only in the database.
package service

import (
	"context"
	"fmt"
	"sync"
	"time"

	"github.com/IBM/sarama"
	"github.com/go-logr/logr"

	"github.com/osac-project/osac-metering/internal/database"
	"github.com/osac-project/osac-metering/internal/fold"
	kafkaconn "github.com/osac-project/osac-metering/internal/kafka"
	"github.com/osac-project/osac-metering/internal/relay"
	"github.com/osac-project/osac-metering/internal/round"
	"github.com/osac-project/osac-metering/internal/rules"
	"github.com/osac-project/osac-metering/internal/usage"
)

// DiscoverInterval is how often metering looks for new tenants and for tenants whose topics
// are gone.
const DiscoverInterval = 30 * time.Second

// Config configures the service.
type Config struct {
	Kafka            kafkaconn.ConnectionConfig
	Store            *database.Store
	Rules            rules.Source
	HeartbeatTick    time.Duration
	DeploymentID     string
	Logger           logr.Logger
}

// Service owns the per-tenant loops.
type Service struct {
	cfg Config

	client   sarama.Client
	consumer sarama.Consumer
	producer sarama.SyncProducer
	engine   *usage.Engine

	lock     sync.Mutex
	tenants  map[string]*tenantLoops
	stopping bool
}

type tenantLoops struct {
	cancel  context.CancelFunc
	stopped chan struct{}
}

// New loads rule revisions, checks them against the ones metering has used, and prepares the
// Kafka clients. It fails when metering must refuse to start.
func New(ctx context.Context, cfg Config) (*Service, error) {
	present, err := cfg.Rules.List(ctx)
	if err != nil {
		return nil, fmt.Errorf("reading rule revisions: %w", err)
	}
	used, err := cfg.Store.ListRuleRevisions(ctx)
	if err != nil {
		return nil, fmt.Errorf("reading recorded rule revisions: %w", err)
	}
	approved, err := rules.CheckStartup(used, present, time.Now)
	if err != nil {
		return nil, fmt.Errorf("checking rule revisions: %w", err)
	}
	for _, revision := range approved {
		if err := cfg.Store.RecordRuleRevision(ctx, database.RuleRevisionRow{
			Revision:    revision.Number,
			EffectiveAt: revision.EffectiveAt,
			MetersYAML:  revision.MetersYAML,
		}); err != nil {
			return nil, err
		}
	}
	engine, err := usage.NewEngine(approved, cfg.DeploymentID)
	if err != nil {
		return nil, err
	}

	brokers := kafkaconn.SplitAndTrim(cfg.Kafka.Brokers, ",")
	sc := sarama.NewConfig()
	sc.Version = sarama.V3_9_0_0
	sc.Producer.Return.Successes = true
	if err := kafkaconn.ConfigureTLS(sc, cfg.Kafka.TLSCACert); err != nil {
		return nil, err
	}
	if err := kafkaconn.ConfigureSASL(sc, cfg.Kafka.SASLUser, cfg.Kafka.SASLPassFile); err != nil {
		return nil, err
	}
	client, err := sarama.NewClient(brokers, sc)
	if err != nil {
		return nil, fmt.Errorf("connecting to kafka: %w", err)
	}
	consumer, err := sarama.NewConsumerFromClient(client)
	if err != nil {
		_ = client.Close()
		return nil, fmt.Errorf("creating kafka consumer: %w", err)
	}
	producer, err := sarama.NewSyncProducerFromClient(client)
	if err != nil {
		_ = consumer.Close()
		return nil, fmt.Errorf("creating kafka producer: %w", err)
	}
	return &Service{
		cfg:      cfg,
		client:   client,
		consumer: consumer,
		producer: producer,
		engine:   engine,
		tenants:  map[string]*tenantLoops{},
	}, nil
}

// Run discovers tenants and runs their loops until the context ends.
func (s *Service) Run(ctx context.Context) error {
	defer func() {
		s.lock.Lock()
		s.stopping = true
		for _, loops := range s.tenants {
			loops.cancel()
		}
		s.lock.Unlock()
		_ = s.producer.Close()
		_ = s.consumer.Close()
		_ = s.client.Close()
	}()

	ticker := time.NewTicker(DiscoverInterval)
	defer ticker.Stop()
	for {
		if err := s.discover(ctx); err != nil {
			return err
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-ticker.C:
		}
	}
}

// discover starts loops for new tenants and stops loops for tenants whose event topic is
// gone. A tenant's topics are deleted only after its final invoice; metering then stops the
// tenant and deletes its rows.
func (s *Service) discover(ctx context.Context) error {
	tenants, err := kafkaconn.ListEventTenants(ctx, s.cfg.Kafka)
	if err != nil {
		return fmt.Errorf("listing tenant event topics: %w", err)
	}
	live := map[string]bool{}

	s.lock.Lock()
	for _, tenant := range tenants {
		live[tenant] = true
		if _, running := s.tenants[tenant]; running {
			continue
		}
		tenantCtx, cancel := context.WithCancel(ctx)
		loops := &tenantLoops{cancel: cancel, stopped: make(chan struct{})}
		s.tenants[tenant] = loops
		go s.runTenant(tenantCtx, tenant, loops)
		s.cfg.Logger.Info("tenant started", "tenant", tenant)
	}
	for tenant, loops := range s.tenants {
		if live[tenant] || s.stopping {
			continue
		}
		loops.cancel()
		delete(s.tenants, tenant)
		go func(tenant string) {
			<-loops.stopped
			if err := s.cfg.Store.DeleteTenantRows(context.Background(), tenant); err != nil {
				s.cfg.Logger.Error(err, "deleting rows of a tenant whose topics are gone", "tenant", tenant)
			}
			s.cfg.Logger.Info("tenant stopped; rows deleted", "tenant", tenant)
		}(tenant)
	}
	s.lock.Unlock()
	return nil
}

// runTenant runs one tenant's fold, round, and relay. Each loop logs its own errors and keeps
// going; a loop that exits ends the tenant only when the context is done.
func (s *Service) runTenant(ctx context.Context, tenant string, loops *tenantLoops) {
	defer close(loops.stopped)

	foldLoop, err := fold.New(s.cfg.Store, s.consumer, s.cfg.Logger.WithValues("loop", "fold"))
	if err != nil {
		s.cfg.Logger.Error(err, "creating fold", "tenant", tenant)
		return
	}
	roundLoop, err := round.New(
		s.cfg.Store, s.engine, s.client, s.consumer,
		s.cfg.Logger.WithValues("loop", "round"),
	)
	if err != nil {
		s.cfg.Logger.Error(err, "creating round", "tenant", tenant)
		return
	}
	relayLoop, err := relay.New(
		s.cfg.Store, s.producer, s.cfg.Logger.WithValues("loop", "relay"),
	)
	if err != nil {
		s.cfg.Logger.Error(err, "creating relay", "tenant", tenant)
		return
	}

	go func() {
		if err := foldLoop.RunTenant(ctx, tenant); err != nil && ctx.Err() == nil {
			s.cfg.Logger.Error(err, "fold exited", "tenant", tenant)
		}
	}()
	go func() {
		if err := roundLoop.RunTenant(ctx, tenant, s.cfg.HeartbeatTick); err != nil && ctx.Err() == nil {
			s.cfg.Logger.Error(err, "round exited", "tenant", tenant)
		}
	}()
	if err := relayLoop.RunTenant(ctx, tenant); err != nil && ctx.Err() == nil {
		s.cfg.Logger.Error(err, "relay exited", "tenant", tenant)
	}
}
